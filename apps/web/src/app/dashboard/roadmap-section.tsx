"use client";

import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  getRoadmap,
  listSavedJobs,
  type RoadmapGapState,
  type RoadmapItem,
  type RoadmapResponse,
  type RoadmapStep,
  type RoadmapStepPhase,
  type RoadmapWeek,
} from "@/lib/api-client";

/** Between 1 and however many jobs the user has actually saved. The
 * server enforces the same bound; this keeps a stale value from ever
 * being sent after a deletion. */
function clampTopN(value: number, savedJobCount: number): number {
  if (savedJobCount < 1) return value;
  return Math.min(Math.max(1, value), savedJobCount);
}

/**
 * The candidate's current four-phase learning roadmap.
 *
 * ONE COMBINED PLAN, not one per job. It answers "what should I work on
 * next, given everything I have saved", and an item routinely names
 * several jobs at once.
 *
 * NOTHING RENDERS UNTIL THE USER ASKS. The roadmap is derived on read
 * and reaches an LLM, so it is generated on an explicit click rather
 * than on page load — a dashboard that quietly regenerates a plan on
 * every render is spending somebody's provider budget on scrolling.
 *
 * READABLE WITHOUT TRUSTING THE MODEL. `why`, the state badge, the
 * affected jobs and the score are computed server-side without a model
 * and are always shown. `task` and `success_criteria` come from one and
 * are simply absent when the narrative was rejected — the plan is
 * thinner, never wrong.
 */
interface RoadmapSectionProps {
  accessToken: string;
  refreshKey?: number;
  /** Bumped by the dashboard when the saved jobs changed — one was
   * added, deleted, reordered or edited.
   *
   * ITS OWN PROP, not folded into `refreshKey`, because it means
   * something `refreshKey` does not. Confirming a skill changes what
   * the NEXT plan would say; changing the saved jobs changes what a
   * plan already on screen is ABOUT — the jobs it names, the order it
   * ranked them in, or the requirements it derived its gaps from. Only
   * this signal can leave the section asking for more jobs than exist,
   * or showing a plan built around one the user has just deleted. */
  jobsVersion?: number;
}

const STATE_LABEL: Record<RoadmapGapState, string> = {
  missing_required: "Missing — required",
  missing_preferred: "Missing — preferred",
  weak_evidence: "Evidence not reviewed",
};

const STATE_CLASS: Record<RoadmapGapState, string> = {
  missing_required: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  missing_preferred:
    "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  weak_evidence: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
};

/** Why the wording is missing. The priorities are unaffected either
 * way, and the copy has to make that clear rather than reading like the
 * whole feature failed. */
const REASON_LABEL: Record<string, string> = {
  no_selected_jobs: "you have not saved any jobs yet",
  malformed_json: "the explanation service returned something unreadable",
  schema_invalid: "the explanation service returned an unexpected shape",
  unknown_evidence_id: "the explanation referred to something not in your data",
  ungrounded_claim: "the explanation could not be tied to your data",
  invented_skill: "the explanation named a skill outside your plan",
  invented_number: "the explanation used a number not in your data",
  contradicts_facts:
    "the wording disagreed with the plan CareerLens calculated",
  disallowed_link: "the explanation included a link",
  response_too_large: "the response was too long",
  provider_timeout: "the explanation service did not answer in time",
  provider_unavailable: "the explanation service was unavailable",
  provider_error: "the explanation service failed unexpectedly",
};

/** Hard ceiling on declared study time, matching the API. Sixteen hours
 * is already an implausible day. */
const MAX_HOURS_PER_DAY = 16;
const DEFAULT_TOP_N = 5;
/** Two days, not seven. Weeks are only a presentation grouping now, so
 * an interview on Thursday is a legitimate plan rather than a
 * validation error. The API enforces the same floor. */
const MIN_DURATION_DAYS = 2;
const MAX_DURATION_DAYS = 56;

/** What each step is FOR. Decided by `roadmap_schedule_v2`, so this
 * badge is readable whether or not a model wrote anything. */
const PHASE_LABEL: Record<RoadmapStepPhase, string> = {
  learn: "Learn",
  practice: "Practise",
  build: "Build",
  prove: "Prove",
  self_check: "Self-check",
  demonstrate: "Demonstrate",
  document: "Document",
};

const PHASE_CLASS: Record<RoadmapStepPhase, string> = {
  learn:
    "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300",
  practice: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  build:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  prove:
    "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
  self_check:
    "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  demonstrate: "bg-teal-100 text-teal-800 dark:bg-teal-950 dark:text-teal-300",
  document: "bg-zinc-200 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-300",
};

function dayRange(startDay: number, endDay: number): string {
  return startDay === endDay
    ? `Day ${startDay}`
    : `Days ${startDay}\u2013${endDay}`;
}

export default function RoadmapSection({
  accessToken,
  refreshKey = 0,
  jobsVersion = 0,
}: RoadmapSectionProps) {
  const [topN, setTopN] = useState(DEFAULT_TOP_N);
  // The ceiling on topN is the user's OWN saved-job count, so it has to
  // be fetched. The roadmap response reports it too, which is what lets
  // the control re-bound itself after a job is added or deleted without
  // a second request.
  const [savedJobCount, setSavedJobCount] = useState<number | null>(null);
  const [durationDays, setDurationDays] = useState(28);
  const [hoursPerDay, setHoursPerDay] = useState(1);
  const [roadmap, setRoadmap] = useState<RoadmapResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Whether the plan on screen predates a change to the saved jobs.
  const [outdated, setOutdated] = useState(false);

  // The count only — never the roadmap. Generating a plan reaches an
  // LLM, so it stays behind an explicit click; knowing how many jobs
  // exist is a cheap read the control needs before the first one.
  //
  // RE-READ WHEN THE COLLECTION CHANGES, which is what `jobsVersion`
  // reports. Without it this ran once and the ceiling went stale the
  // moment a job was deleted: the input still offered "5 of 5 saved
  // jobs" against two that existed, every Generate came back 422, and
  // because the count only healed on a SUCCESSFUL response there was no
  // way out but reloading the page.
  useEffect(() => {
    let cancelled = false;
    listSavedJobs(accessToken)
      .then((jobs) => {
        if (cancelled) return;
        setSavedJobCount(jobs.length);
        setTopN((current) => clampTopN(current, jobs.length));
      })
      .catch(() => {
        // A failed count is not worth an error banner: the input falls
        // back to unbounded and the API rejects anything impossible.
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken, refreshKey, jobsVersion]);

  // A PLAN IS ABOUT THE JOBS IT WAS BUILT FROM. When those change it
  // stops describing the user's actual situation — it can name a job
  // they deleted, or rank by an order they have since changed — so it
  // is withdrawn rather than left on screen looking current.
  //
  // The mount run is skipped by comparing against the version this
  // component started with: arriving on the page is not a change, and
  // there is nothing rendered to retire anyway.
  const seenJobsVersion = useRef(jobsVersion);
  useEffect(() => {
    if (seenJobsVersion.current === jobsVersion) return;
    seenJobsVersion.current = jobsVersion;
    setOutdated(true);
  }, [jobsVersion]);

  async function generate() {
    setLoading(true);
    setError(null);
    try {
      const next = await getRoadmap(accessToken, {
        topN,
        durationDays,
        hoursPerDay,
      });
      setRoadmap(next);
      // Built from the jobs as they are now, so whatever made the last
      // one stale no longer applies.
      setOutdated(false);
      // Self-healing: if a job was deleted in another tab, the ceiling
      // and the input correct themselves here.
      setSavedJobCount(next.saved_job_count);
      setTopN((current) => clampTopN(current, next.saved_job_count));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section
      aria-labelledby="roadmap-heading"
      className="rounded border border-zinc-200 p-4 dark:border-zinc-800"
    >
      <h2
        id="roadmap-heading"
        className="text-sm font-semibold text-black dark:text-zinc-50"
      >
        Learning roadmap
      </h2>
      <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
        A day-by-day plan built from your saved jobs and the gaps in your
        evidence. CareerLens decides what to work on and when; the written
        guidance turns that into steps. It changes as your jobs and skills
        change.
      </p>

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label
            htmlFor="roadmap-top-n"
            className="text-xs font-medium text-black dark:text-zinc-50"
          >
            Jobs to prepare for
          </label>
          <div className="flex items-center gap-2">
            <input
              id="roadmap-top-n"
              type="number"
              min={1}
              max={savedJobCount ?? undefined}
              value={topN}
              onChange={(event) =>
                setTopN(
                  clampTopN(Number(event.target.value), savedJobCount ?? 0),
                )
              }
              className="w-20 rounded border border-zinc-300 px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
            />
            <span className="text-xs text-zinc-600 dark:text-zinc-400">
              of {savedJobCount ?? "—"} saved{" "}
              {savedJobCount === 1 ? "job" : "jobs"}
            </span>
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor="roadmap-days"
            className="text-xs font-medium text-black dark:text-zinc-50"
          >
            Days available
          </label>
          <input
            id="roadmap-days"
            type="number"
            min={MIN_DURATION_DAYS}
            max={MAX_DURATION_DAYS}
            value={durationDays}
            onChange={(event) => setDurationDays(Number(event.target.value))}
            className="w-24 rounded border border-zinc-300 px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor="roadmap-hours"
            className="text-xs font-medium text-black dark:text-zinc-50"
          >
            Hours per day
          </label>
          <input
            id="roadmap-hours"
            type="number"
            min={0.5}
            max={MAX_HOURS_PER_DAY}
            step={0.5}
            value={hoursPerDay}
            onChange={(event) => setHoursPerDay(Number(event.target.value))}
            className="w-24 rounded border border-zinc-300 px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
          />
        </div>

        <button
          type="button"
          onClick={generate}
          disabled={loading}
          className="rounded bg-black px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50 dark:bg-zinc-50 dark:text-black"
        >
          {loading ? "Building…" : "Generate roadmap"}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-3 text-xs text-red-700 dark:text-red-400">
          {error}
        </p>
      )}

      {/* WITHDRAWN, NOT QUIETLY LEFT UP. The alternative was showing a
          plan that still lists a job the user has just deleted as one
          of their selected jobs, which is a false statement about their
          own data — the same thing app/api/v1/roadmap.py refuses to
          persist a plan for. Saying why, and what to do about it, beats
          both silently clearing it and silently keeping it. */}
      {roadmap && outdated ? (
        <p
          role="status"
          className="mt-3 text-xs text-amber-800 dark:text-amber-300"
        >
          Your saved jobs changed after this plan was made, so it is no longer
          shown — it was built from the jobs you had before. Generate the
          roadmap again for an up-to-date plan.
        </p>
      ) : (
        roadmap && <RoadmapPlan roadmap={roadmap} />
      )}
    </section>
  );
}

function RoadmapPlan({ roadmap }: { roadmap: RoadmapResponse }) {
  if (!roadmap.has_selected_jobs) {
    return (
      <p className="mt-3 text-xs text-zinc-600 dark:text-zinc-400">
        You have not saved any jobs yet. Save a posting and the roadmap will
        show what to work on first.
      </p>
    );
  }

  return (
    <div className="mt-4 flex flex-col gap-4">
      {roadmap.overview && (
        <p className="text-sm text-black dark:text-zinc-50">
          {roadmap.overview}
        </p>
      )}

      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        {roadmap.selected_job_count} of {roadmap.saved_job_count} saved{" "}
        {roadmap.saved_job_count === 1 ? "job" : "jobs"} ·{" "}
        {roadmap.duration_days} days · {roadmap.hours_per_day} hours/day · about{" "}
        {roadmap.total_hours} hours in total{" "}
        <span className="text-zinc-500 dark:text-zinc-500">(estimated)</span>
      </p>

      {/* HONEST, NOT APOLOGETIC. No single skill is stretched past a
          fortnight, so a short list of gaps cannot fill a long window.
          Saying so beats padding the plan with work the candidate's own
          jobs never asked for. */}
      {roadmap.coverage === "partial" && roadmap.scheduled_days > 0 && (
        <p className="text-xs text-zinc-600 dark:text-zinc-400">
          Your saved jobs justify {roadmap.scheduled_days} days of focused work,
          so {roadmap.unscheduled_days} of the {roadmap.duration_days} you asked
          for are left unscheduled. Rather than stretch these skills to fill the
          time, use it to go deeper on what is below — or save more jobs and
          generate again.
        </p>
      )}

      {roadmap.narrative_status === "rejected" && (
        <p className="text-xs text-amber-800 dark:text-amber-300">
          {/* Same reordering as the explanation panel: the plan below is
              the product, the wording is the optional extra. Opening with
              the failure made a working feature look broken. */}
          Your priorities and schedule below are complete — they are calculated
          from your own saved jobs and evidence, not written by a model. The
          written guidance could not be produced this time —{" "}
          {REASON_LABEL[roadmap.reason ?? ""] ?? "it could not be produced"}.
        </p>
      )}

      {roadmap.weeks.map((week) => (
        <WeekBlock key={week.week} week={week} />
      ))}
    </div>
  );
}

function WeekBlock({ week }: { week: RoadmapWeek }) {
  return (
    <div className="border-t border-zinc-200 pt-3 dark:border-zinc-800">
      <h3 className="text-xs font-semibold text-black dark:text-zinc-50">
        {week.label}
        {week.focus && (
          <span className="ml-2 font-normal text-zinc-600 dark:text-zinc-400">
            {week.focus}
          </span>
        )}
      </h3>

      {week.items.length === 0 ? (
        <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
          Nothing scheduled for this week — your saved jobs did not produce
          enough to fill it.
        </p>
      ) : (
        <>
          <ul className="mt-2 flex flex-col gap-3">
            {week.items.map((item) => (
              <RoadmapEntry key={item.item_id} item={item} />
            ))}
          </ul>
          {/* The end-of-week check: a capability the candidate can test
              themselves against, not a box to tick. */}
          {week.checkpoint && (
            <p className="mt-3 rounded bg-zinc-100 p-2 text-xs text-black dark:bg-zinc-900 dark:text-zinc-50">
              ✓ {week.checkpoint}
            </p>
          )}
        </>
      )}
    </div>
  );
}

/**
 * One block of days: when, what mode, what to do, and how you know you
 * finished it.
 *
 * THIS IS WHAT ANSWERS "WHAT SHOULD I DO TODAY". Before it existed, a
 * fortnight-long priority rendered as a single instruction and the
 * weeks in between looked empty.
 */
function StepRow({ step }: { step: RoadmapStep }) {
  return (
    <li className="border-l-2 border-zinc-200 pl-3 dark:border-zinc-800">
      <div className="flex flex-wrap items-baseline gap-2">
        <span
          className={`rounded px-1.5 py-0.5 text-[11px] ${PHASE_CLASS[step.phase]}`}
        >
          {PHASE_LABEL[step.phase]}
        </span>
        <span className="text-[11px] text-zinc-500 dark:text-zinc-500">
          {dayRange(step.start_day, step.end_day)} · about{" "}
          {step.estimated_hours} hours (estimated)
        </span>
      </div>
      {step.task && (
        <p className="mt-1 text-xs text-black dark:text-zinc-50">{step.task}</p>
      )}
      {step.done_when && (
        <p className="mt-0.5 text-[11px] text-zinc-600 dark:text-zinc-400">
          Done when: {step.done_when}
        </p>
      )}
    </li>
  );
}

/** One collapsed detail. Native <details> for the keyboard support and
 * the expanded-state announcement, matching the jobs list. */
function Detail({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-[11px] text-zinc-600 dark:text-zinc-400">
        {label}
      </summary>
      <div className="mt-1 text-xs text-zinc-700 dark:text-zinc-300">
        {children}
      </div>
    </details>
  );
}

/**
 * One item, answering four questions in the order a person asks them:
 * what am I doing, what will I have, who is it for — and only then, on
 * request, why and how it was chosen.
 *
 * THE COLLAPSED CARD IS THE PRODUCT. Everything behind a disclosure is
 * still there and still checkable; it is simply not what a candidate
 * reads first. The previous layout showed all of it at once, which read
 * as a report about the user rather than advice to them.
 */
function RoadmapEntry({ item }: { item: RoadmapItem }) {
  const lead = item.affected_jobs[0];
  const others = item.affected_jobs.length - 1;

  return (
    <li className="rounded border border-zinc-200 p-3 dark:border-zinc-800">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="flex items-center gap-2">
          <span className="text-sm font-medium text-black dark:text-zinc-50">
            {item.skill_name}
          </span>
          {/* Kept on the collapsed card rather than buried: "missing"
              and "evidence not reviewed" call for different work, and
              that is mentoring information, not metadata. */}
          <span
            className={`rounded px-1.5 py-0.5 text-[11px] ${STATE_CLASS[item.state]}`}
          >
            {STATE_LABEL[item.state]}
          </span>
        </span>
        <span className="text-[11px] text-zinc-500 dark:text-zinc-500">
          {dayRange(item.start_day, item.end_day)} · about{" "}
          {item.estimated_hours} hours (estimated)
        </span>
      </div>

      {/* WHAT TO DO */}
      {item.task && (
        <p className="mt-1 text-xs text-black dark:text-zinc-50">{item.task}</p>
      )}
      {/* WHAT YOU END UP WITH */}
      {item.outcome && (
        <p className="mt-1 text-xs text-zinc-700 dark:text-zinc-300">
          You&rsquo;ll finish with: {item.outcome}
        </p>
      )}
      {/* WHO IT IS FOR — one line; the full list is a disclosure below. */}
      {lead && (
        <p className="mt-1 text-[11px] text-zinc-600 dark:text-zinc-400">
          Helps {lead.company} (#{lead.priority_rank})
          {others > 0 &&
            ` and ${others} other ${others === 1 ? "job" : "jobs"}`}
        </p>
      )}

      {/* THE DAY-BY-DAY ANSWER. The days and the phase are computed;
          only the sentence inside each one is written. A rejected
          narrative therefore still leaves a real schedule to follow. */}
      {item.steps.length > 0 && (
        <ol className="mt-2 flex flex-col gap-2">
          {item.steps.map((step) => (
            <StepRow key={step.step_id} step={step} />
          ))}
        </ol>
      )}

      <Detail label="Why this?">{item.why}</Detail>

      {item.success_criteria && (
        <Detail label="What you should be able to do">
          {item.success_criteria}
        </Detail>
      )}

      <Detail label={`Jobs this helps (${item.affected_jobs.length})`}>
        <ul className="flex flex-col gap-1">
          {item.affected_jobs.map((job) => (
            <li key={job.saved_job_id}>
              #{job.priority_rank} {job.company} — {job.title}
              {job.match_score !== null && ` (${job.match_score}% skill match)`}
            </li>
          ))}
        </ul>
      </Detail>

      <Detail label="Technical details">
        {/* The raw terms, not a second copy of the badge above: the
            score is reproducible by hand from exactly these two
            numbers, which is what makes it checkable. */}
        <p>
          <code>{item.state}</code> · priority {item.score} ={" "}
          {item.state_weight} (gap kind) + {item.recurrence} (your job
          priorities)
        </p>
        {item.evidence.length > 0 && (
          <ul className="mt-1 flex flex-col gap-1">
            {item.evidence.map((row) => (
              <li
                key={row.evidence_id}
                className="border-l-2 border-zinc-300 pl-2 dark:border-zinc-700"
              >
                <span className="font-medium">{row.source_type}</span>
                {row.excerpt ? `: “${row.excerpt}”` : null}
              </li>
            ))}
          </ul>
        )}
      </Detail>
    </li>
  );
}
