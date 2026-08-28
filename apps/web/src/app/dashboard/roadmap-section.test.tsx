import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RoadmapSection from "./roadmap-section";
import type { RoadmapResponse } from "@/lib/api-client";

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return { ...actual, getRoadmap: vi.fn(), listSavedJobs: vi.fn() };
});

const { getRoadmap, listSavedJobs } = await import("@/lib/api-client");

function roadmap(overrides: Partial<RoadmapResponse> = {}): RoadmapResponse {
  return {
    formula_version: "roadmap_priority_v1",
    schedule_version: "roadmap_schedule_v2",
    narrative_schema_version: "roadmap_narrative_v1",
    narrative_status: "generated",
    reason: null,
    provider: "mock",
    selected_job_count: 2,
    saved_job_count: 4,
    has_selected_jobs: true,
    duration_days: 28,
    hours_per_day: 1,
    total_hours: 28,
    scheduled_days: 28,
    unscheduled_days: 0,
    coverage: "full",
    overview: "A plan for the days ahead.",
    weeks: [
      {
        week: 1,
        label: "Week 1 · Days 1–7",
        start_day: 1,
        end_day: 7,
        focus: "Working on AWS",
        checkpoint:
          "By the end of this week you should be able to explain what you built with AWS.",
        items: [
          {
            item_id: "skill-1",
            skill_id: "skill-1",
            skill_name: "AWS",
            state: "missing_required",
            start_day: 1,
            end_day: 4,
            week: 1,
            score: 109,
            state_weight: 100,
            recurrence: 9,
            why: "AWS is missing and is required by 2 of your 2 selected jobs, including your #1 priority (Acme — Engineer).",
            affected_jobs: [
              {
                saved_job_id: "job-1",
                title: "Engineer",
                company: "Acme",
                priority_rank: 1,
                match_score: 62,
              },
            ],
            evidence: [],
            estimated_hours: 4,
            steps: [
              {
                step_id: "skill-1:1",
                phase: "learn",
                start_day: 1,
                end_day: 2,
                week: 1,
                estimated_hours: 2,
                task: "Read how identity and access are modelled, and write one policy by hand.",
                done_when: "You can explain what that policy grants.",
              },
              {
                step_id: "skill-1:2",
                phase: "build",
                start_day: 3,
                end_day: 4,
                week: 1,
                estimated_hours: 2,
                task: "Deploy one small service of your own and lock its access down.",
                done_when: "The service answers on a URL you can share.",
              },
            ],
            task: "Deploy a small service to AWS and document it.",
            outcome: "A running service and a README.",
            success_criteria: "Someone else can follow your write-up.",
          },
        ],
      },
      {
        week: 2,
        label: "Week 2 · Days 8–14",
        start_day: 8,
        end_day: 14,
        focus: null,
        checkpoint: null,
        items: [],
      },
    ],
    ...overrides,
  };
}

describe("RoadmapSection", () => {
  beforeEach(() => {
    vi.mocked(getRoadmap).mockReset();
    vi.mocked(listSavedJobs).mockReset();
    vi.mocked(listSavedJobs).mockResolvedValue([]);
  });

  it("generates nothing until the user asks", () => {
    render(<RoadmapSection accessToken="token" />);

    expect(getRoadmap).not.toHaveBeenCalled();
  });

  it("shows the plan, the reason and the affected jobs", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(await screen.findByText("AWS")).toBeInTheDocument();
    // The mentoring answer comes first: what to do, what you get, who
    // it helps, and when.
    expect(
      screen.getByText(/Deploy a small service to AWS/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/A running service and a README/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Helps Acme \(#1\)/)).toBeInTheDocument();
    expect(screen.getByText(/Days 1–4/)).toBeInTheDocument();
    // The gap kind stays visible: "missing" and "not reviewed" call for
    // different work, which is advice rather than metadata.
    expect(screen.getByText(/Missing — required/)).toBeInTheDocument();
  });

  it("labels effort as an estimate rather than a duration", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(
      await screen.findByText(/about 4 hours \(estimated\)/),
    ).toBeInTheDocument();
  });

  it("puts the reasoning and the job list behind disclosures", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    // Present and reachable, but not part of the primary prose.
    expect(await screen.findByText("Why this?")).toBeInTheDocument();
    expect(
      screen.getByText("What you should be able to do"),
    ).toBeInTheDocument();
    expect(screen.getByText("Jobs this helps (1)")).toBeInTheDocument();
    expect(screen.getByText("Technical details")).toBeInTheDocument();
  });

  it("shows the end-of-week checkpoint", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(
      await screen.findByText(/By the end of this week you should be able to/),
    ).toBeInTheDocument();
  });

  it("bounds the job count by what the user has actually saved", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    const input = await screen.findByLabelText(/jobs to prepare for/i);
    // The default of 5 clamps down to the two jobs that exist.
    await waitFor(() => expect(input).toHaveValue(2));
    expect(input).toHaveAttribute("max", "2");
    expect(screen.getByText(/of 2 saved jobs/)).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "9" } });
    expect(input).toHaveValue(2);
  });

  it("caps hours per day at sixteen", async () => {
    render(<RoadmapSection accessToken="token" />);

    expect(screen.getByLabelText(/hours per day/i)).toHaveAttribute(
      "max",
      "16",
    );
  });

  it("keeps the priorities when the narrative was rejected", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(
      roadmap({
        narrative_status: "rejected",
        reason: "provider_timeout",
        overview: null,
        weeks: roadmap().weeks.map((week) => ({
          ...week,
          focus: null,
          checkpoint: null,
          items: week.items.map((item) => ({
            ...item,
            task: null,
            outcome: null,
            success_criteria: null,
          })),
        })),
      }),
    );
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    // The wording is gone and says why...
    expect(
      await screen.findByText(/did not answer in time/),
    ).toBeInTheDocument();
    // ...and every deterministic priority is still on the page.
    expect(screen.getByText("AWS")).toBeInTheDocument();
    expect(
      screen.getByText(/required by 2 of your 2 selected jobs/),
    ).toBeInTheDocument();
  });

  it("says the input is empty rather than claiming no gaps", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(
      roadmap({
        has_selected_jobs: false,
        selected_job_count: 0,
        weeks: [],
      }),
    );
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(
      await screen.findByText(/have not saved any jobs yet/),
    ).toBeInTheDocument();
  });

  // --- 6.4b: the day-by-day view ---------------------------------------

  it("shows each step with its phase, its days and what to do", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    // The deterministic half: the mode and the days, rendered without
    // trusting a generated word.
    expect(await screen.findByText("Learn")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText(/Days 1–2/)).toBeInTheDocument();
    // The written half: what to actually do, and the finish line.
    expect(
      screen.getByText(/Read how identity and access are modelled/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Done when: The service answers on a URL/),
    ).toBeInTheDocument();
  });

  it("keeps the steps when the wording was rejected", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(
      roadmap({
        narrative_status: "rejected",
        reason: "contradicts_facts",
        overview: null,
        weeks: roadmap().weeks.map((week) => ({
          ...week,
          focus: null,
          checkpoint: null,
          items: week.items.map((item) => ({
            ...item,
            task: null,
            outcome: null,
            success_criteria: null,
            steps: item.steps.map((step) => ({
              ...step,
              task: null,
              done_when: null,
            })),
          })),
        })),
      }),
    );
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(
      await screen.findByText(/disagreed with the plan CareerLens calculated/),
    ).toBeInTheDocument();
    // The schedule is deterministic, so it survives whole.
    expect(screen.getByText("Learn")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText(/Days 3–4/)).toBeInTheDocument();
  });

  it("states unscheduled days rather than padding the plan", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(
      roadmap({
        duration_days: 56,
        total_hours: 56,
        scheduled_days: 28,
        unscheduled_days: 28,
        coverage: "partial",
      }),
    );
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(
      await screen.findByText(/justify 28 days of focused work/),
    ).toBeInTheDocument();
  });

  it("says nothing about coverage when the plan fills the window", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    await screen.findByText("AWS");
    expect(screen.queryByText(/left unscheduled/)).not.toBeInTheDocument();
  });

  it("accepts a two-day plan", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    const days = screen.getByLabelText(/days available/i);
    expect(days).toHaveAttribute("min", "2");

    fireEvent.change(days, { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    await waitFor(() =>
      expect(getRoadmap).toHaveBeenCalledWith("token", {
        topN: 5,
        durationDays: 2,
        hoursPerDay: 1,
        narrate: false,
      }),
    );
  });

  it("passes the chosen top-n and time to the API", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

    fireEvent.change(screen.getByLabelText(/jobs to prepare for/i), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    await waitFor(() =>
      expect(getRoadmap).toHaveBeenCalledWith("token", {
        topN: 3,
        durationDays: 28,
        hoursPerDay: 1,
        narrate: false,
      }),
    );
  });
});

// =====================================================================
// 7.1c — the saved-job collection changing underneath a rendered plan
//
// THE REPORTED BUG. `savedJobCount` was read once on mount, so deleting
// a job left the section offering "5 of 5 saved jobs" against two that
// existed. Every Generate came back 422, and because the count only
// healed on a SUCCESSFUL response there was no way out but reloading.
//
// `jobsVersion` is the dashboard's signal that the collection moved —
// added, deleted or reordered. See page.tsx.
// =====================================================================

describe("RoadmapSection when the saved jobs change", () => {
  beforeEach(() => {
    vi.mocked(getRoadmap).mockReset();
    vi.mocked(listSavedJobs).mockReset();
  });

  it("shows the count the dashboard reports when a job is deleted", async () => {
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={4} jobsVersion={0} />,
    );
    expect(await screen.findByText(/of 4 saved jobs/)).toBeInTheDocument();

    rerender(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={1} />,
    );

    expect(await screen.findByText(/of 2 saved jobs/)).toBeInTheDocument();
  });

  it("clamps the chosen job count down to what is left", async () => {
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={4} jobsVersion={0} />,
    );
    const input = await screen.findByLabelText(/jobs to prepare for/i);
    await waitFor(() => expect(input).toHaveValue(4));

    rerender(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={1} />,
    );

    await waitFor(() => expect(input).toHaveValue(2));
    expect(input).toHaveAttribute("max", "2");
  });

  it("never asks for more jobs than the user still has", async () => {
    // THE 422 ITSELF. Before the fix this sent top_n=4 against two saved
    // jobs, the API refused it, and the section could not recover. The
    // guard is now a clamp against the reported count rather than a
    // refetch, and it has to hold just as firmly.
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={4} jobsVersion={0} />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText(/jobs to prepare for/i)).toHaveValue(4),
    );

    rerender(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={1} />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText(/jobs to prepare for/i)).toHaveValue(2),
    );
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    await waitFor(() =>
      expect(getRoadmap).toHaveBeenCalledWith("token", {
        topN: 2,
        durationDays: 28,
        hoursPerDay: 1,
        narrate: false,
      }),
    );
  });

  it("withdraws a plan built from jobs that have since changed", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={0} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
    expect(await screen.findByText("AWS")).toBeInTheDocument();

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);

    // The plan named "Acme — Engineer" as a selected job; that claim is
    // no longer known to be true, so none of it stays on screen.
    await waitFor(() => expect(screen.queryByText("AWS")).toBeNull());
    expect(screen.getByRole("status")).toHaveTextContent(
      /saved jobs changed after this plan was made/i,
    );
  });

  it("shows the fresh plan again once it is regenerated", async () => {
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={0} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
    expect(await screen.findByText("AWS")).toBeInTheDocument();

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);
    await waitFor(() => expect(screen.queryByText("AWS")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    expect(await screen.findByText("AWS")).toBeInTheDocument();
    expect(
      screen.queryByText(/saved jobs changed after this plan was made/i),
    ).toBeNull();
  });

  it("says nothing about staleness before anything was generated", async () => {
    // Arriving on the page is not a change, and there is no plan to
    // retire — the mount run of the effect must stay silent.
    render(
      <RoadmapSection accessToken="token" savedJobCount={1} jobsVersion={3} />,
    );

    expect(await screen.findByText(/of 1 saved job/)).toBeInTheDocument();
    expect(
      screen.queryByText(/saved jobs changed after this plan was made/i),
    ).toBeNull();
  });

  it("never lists the saved jobs itself (7.2 F5)", async () => {
    // THE PERFORMANCE GUARD, restated. This section used to fetch the
    // list purely to learn its length — a duplicate of JobsSection's own
    // request on every dashboard load, and another one on every skill or
    // qualification change. The count is a prop now, so the correct
    // number of requests from here is zero, in every one of the states
    // that used to trigger one.
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={1} jobsVersion={0} />,
    );
    expect(await screen.findByText(/of 1 saved job/)).toBeInTheDocument();

    rerender(
      <RoadmapSection accessToken="token" savedJobCount={1} jobsVersion={0} />,
    );
    rerender(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={1} />,
    );
    expect(await screen.findByText(/of 2 saved jobs/)).toBeInTheDocument();

    expect(listSavedJobs).not.toHaveBeenCalled();
  });

  it("shows the count the dashboard reports when a job is added", async () => {
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={1} jobsVersion={0} />,
    );
    expect(await screen.findByText(/of 1 saved job/)).toBeInTheDocument();

    rerender(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={1} />,
    );

    expect(await screen.findByText(/of 2 saved jobs/)).toBeInTheDocument();
  });

  it("retires a plan after a reorder, whose priorities have moved", async () => {
    // The count is identical; `roadmap_priority_v1` reads the ORDER, so
    // the ranks in a plan drawn before the move are no longer the
    // user's. `savedJobCount` deliberately does not move here — this is
    // exactly the case a count-based signal cannot see, which is why
    // `jobsVersion` remains its own prop.
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={0} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
    expect(await screen.findByText("AWS")).toBeInTheDocument();

    rerender(
      <RoadmapSection accessToken="token" savedJobCount={2} jobsVersion={1} />,
    );

    await waitFor(() => expect(screen.queryByText("AWS")).toBeNull());
    expect(screen.getByRole("status")).toHaveTextContent(
      /generate the roadmap again/i,
    );
  });
});
// =====================================================================
// 7.2 F2 — the schedule must not wait for the model
//
// The route used to compute the whole deterministic plan and then hold
// it behind a narrative that can run to `explanation_timeout_seconds`
// (180) against a local model producing roughly ten tokens a second.
// The section now asks for the plan alone, paints it, and asks for the
// wording separately.
//
// THE ARCHITECTURE THESE PROTECT: CareerLens knows the plan, Ollama
// adds mentoring. Every test below is written so that it fails if the
// user is ever made to wait for prose before seeing the schedule.
// =====================================================================

describe("RoadmapSection deterministic-first generation", () => {
  beforeEach(() => {
    vi.mocked(getRoadmap).mockReset();
  });

  /** The real shape of the two-phase flow: the deterministic request
   * resolves at once, the narrative is held open by the test. */
  function deterministicThenPendingNarrative(
    plan: RoadmapResponse = roadmap({
      narrative_status: "skipped",
      overview: null,
      saved_job_count: 2,
    }),
  ) {
    let settle: (value: RoadmapResponse) => void = () => {};
    let reject: (reason: unknown) => void = () => {};
    vi.mocked(getRoadmap).mockImplementation((_token, options) =>
      options.narrate === false
        ? Promise.resolve(plan)
        : new Promise<RoadmapResponse>((resolve, rejectIt) => {
            settle = resolve;
            reject = rejectIt;
          }),
    );
    return {
      settleNarrative: (value: RoadmapResponse) => settle(value),
      failNarrative: (reason: unknown) => reject(reason),
    };
  }

  function generate() {
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
  }

  it("renders the schedule before the narrative has resolved", async () => {
    // THE WHOLE POINT. The narrative promise is still open for the
    // entirety of this test, and the plan is already readable.
    deterministicThenPendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();

    expect(await screen.findByText("AWS")).toBeInTheDocument();
    // Not just the item name — the substance a user acts on.
    expect(screen.getByText(/Missing — required/)).toBeInTheDocument();
    expect(screen.getByText(/Days 1–4/)).toBeInTheDocument();
    expect(screen.getByText(/Helps Acme \(#1\)/)).toBeInTheDocument();
    expect(screen.getByText("Why this?")).toBeInTheDocument();
  });

  it("asks for the plan without a narrative first, then for the wording", async () => {
    deterministicThenPendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();
    await screen.findByText("AWS");

    await waitFor(() => expect(getRoadmap).toHaveBeenCalledTimes(2));
    // ORDER MATTERS: deterministic first, or the user waits.
    expect(vi.mocked(getRoadmap).mock.calls[0][1].narrate).toBe(false);
    expect(vi.mocked(getRoadmap).mock.calls[1][1].narrate).toBe(true);
    // And the second asks about the SAME plan — a different top_n or
    // window would narrate something other than what is on screen.
    const [first, second] = vi.mocked(getRoadmap).mock.calls;
    expect({ ...second[1], narrate: undefined }).toEqual({
      ...first[1],
      narrate: undefined,
    });
  });

  it("says the guidance is still being written, without implying a fault", async () => {
    deterministicThenPendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();
    await screen.findByText("AWS");

    expect(
      screen.getByText(/Written mentoring guidance is being added/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/you can start reading now/i)).toBeInTheDocument();
    // NOT a failure message — nothing has failed.
    expect(screen.queryByText(/could not be produced this time/i)).toBeNull();
  });

  it("replaces the pending state with the guidance when it arrives", async () => {
    const pending = deterministicThenPendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();
    await screen.findByText("AWS");
    expect(
      screen.getByText(/Written mentoring guidance is being added/i),
    ).toBeInTheDocument();

    await act(async () => {
      pending.settleNarrative(roadmap({ saved_job_count: 2 }));
    });

    // The prose is in, the pending line is gone, and the schedule that
    // was already on screen is still there.
    expect(
      await screen.findByText(/Deploy a small service to AWS/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Written mentoring guidance is being added/i),
    ).toBeNull();
    expect(screen.getByText("AWS")).toBeInTheDocument();
    expect(screen.getByText(/Days 1–4/)).toBeInTheDocument();
  });

  it("keeps the whole plan usable when the narrative request fails", async () => {
    // OLLAMA UNAVAILABLE, OR TIMED OUT. Both arrive here as a rejected
    // request, and neither may cost the user the schedule.
    const pending = deterministicThenPendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();
    await screen.findByText("AWS");

    await act(async () => {
      pending.failNarrative(new Error("ollama went away"));
    });

    await waitFor(() =>
      expect(
        screen.getByText(/could not be produced this time/i),
      ).toBeInTheDocument(),
    );
    // THE PLAN SURVIVES INTACT.
    expect(screen.getByText("AWS")).toBeInTheDocument();
    expect(screen.getByText(/Missing — required/)).toBeInTheDocument();
    expect(screen.getByText(/Days 1–4/)).toBeInTheDocument();
    // And it is not reported as a page-level error over a working plan.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("still honours a narrative the API itself rejected", async () => {
    // A DIFFERENT FAILURE, SAME LOSS. The request succeeded and the
    // model's answer failed validation server-side.
    vi.mocked(getRoadmap).mockImplementation((_token, options) =>
      Promise.resolve(
        options.narrate === false
          ? roadmap({ narrative_status: "skipped", overview: null })
          : roadmap({
              narrative_status: "rejected",
              reason: "provider_timeout",
              overview: null,
            }),
      ),
    );
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();

    expect(
      await screen.findByText(/did not answer in time/i),
    ).toBeInTheDocument();
    expect(screen.getByText("AWS")).toBeInTheDocument();
  });

  it("makes exactly one deterministic and one narrative request per click", async () => {
    const pending = deterministicThenPendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();
    await screen.findByText("AWS");
    await waitFor(() => expect(getRoadmap).toHaveBeenCalledTimes(2));

    await act(async () => {
      pending.settleNarrative(roadmap({ saved_job_count: 2 }));
    });
    await screen.findByText(/Deploy a small service to AWS/);

    // NO LOOP. Swapping the narrated plan in must not retrigger either
    // request.
    expect(getRoadmap).toHaveBeenCalledTimes(2);
    const narrateFlags = vi
      .mocked(getRoadmap)
      .mock.calls.map((call) => call[1].narrate);
    expect(narrateFlags).toEqual([false, true]);
  });

  it("spends no model run narrating an empty plan", async () => {
    // Nothing is selected, so there is nothing to write about and
    // asking would burn a generation to be told so.
    vi.mocked(getRoadmap).mockResolvedValue(
      roadmap({
        has_selected_jobs: false,
        selected_job_count: 0,
        saved_job_count: 0,
        narrative_status: "skipped",
        overview: null,
        weeks: [],
      }),
    );
    render(<RoadmapSection accessToken="token" savedJobCount={0} />);

    generate();

    expect(
      await screen.findByText(/have not saved any jobs yet/i),
    ).toBeInTheDocument();
    expect(getRoadmap).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getRoadmap).mock.calls[0][1].narrate).toBe(false);
  });

  it("does not let a superseded narrative overwrite a newer plan", async () => {
    // The button is released once the schedule lands, so a user can
    // regenerate while a model is still working on the previous run.
    const settles: ((value: RoadmapResponse) => void)[] = [];
    vi.mocked(getRoadmap).mockImplementation((_token, options) =>
      options.narrate === false
        ? Promise.resolve(
            roadmap({ narrative_status: "skipped", overview: null }),
          )
        : new Promise<RoadmapResponse>((resolve) => settles.push(resolve)),
    );
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    generate();
    await screen.findByText("AWS");
    await waitFor(() => expect(settles).toHaveLength(1));

    generate();
    await waitFor(() => expect(settles).toHaveLength(2));

    // The FIRST run's narrative comes back late and must be ignored.
    await act(async () => {
      settles[0](
        roadmap({
          overview: "Stale wording from a run the user replaced.",
        }),
      );
    });

    expect(
      screen.queryByText(/Stale wording from a run the user replaced/),
    ).toBeNull();
  });
});

// =====================================================================
// 7.2 F3 (Pass A), now attached to the wait that actually matters
//
// The counter moved from the deterministic request to the narrative
// one. That is the same guarantee in a better place: the fast request
// never needed a stopwatch, and the model is the thing that can run for
// minutes. The Pass A assertions are unchanged in substance — count,
// threshold note, and a clean stop on both outcomes.
// =====================================================================

describe("RoadmapSection elapsed-time feedback", () => {
  beforeEach(() => {
    vi.mocked(getRoadmap).mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function pendingNarrative() {
    let settle: (value: RoadmapResponse) => void = () => {};
    let reject: (reason: unknown) => void = () => {};
    vi.mocked(getRoadmap).mockImplementation((_token, options) =>
      options.narrate === false
        ? Promise.resolve(
            roadmap({
              narrative_status: "skipped",
              overview: null,
              saved_job_count: 2,
            }),
          )
        : new Promise<RoadmapResponse>((resolve, rejectIt) => {
            settle = resolve;
            reject = rejectIt;
          }),
    );
    return {
      settleNarrative: (value: RoadmapResponse) => settle(value),
      failNarrative: (reason: unknown) => reject(reason),
    };
  }

  async function generateAndWaitForPlan() {
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
    await screen.findByText("AWS");
  }

  it("counts the seconds the model has been writing", async () => {
    pendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    await generateAndWaitForPlan();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /Writing mentoring guidance… 0s/,
    );

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      /Writing mentoring guidance… 2s/,
    );
  });

  it("explains a long wait without implying the plan failed", async () => {
    pendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    await generateAndWaitForPlan();
    await screen.findByRole("status");

    await act(async () => {
      vi.advanceTimersByTime(21000);
    });

    expect(screen.getByText(/runs on a local model/i)).toBeInTheDocument();
    // And the plan it is not blocking is still right there.
    expect(screen.getByText("AWS")).toBeInTheDocument();
  });

  it("clears the counter once the guidance arrives", async () => {
    const pending = pendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    await generateAndWaitForPlan();
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    await act(async () => {
      pending.settleNarrative(roadmap({ saved_job_count: 2 }));
    });

    await waitFor(() =>
      expect(screen.queryByText(/Writing mentoring guidance…/)).toBeNull(),
    );
    expect(screen.getByText("AWS")).toBeInTheDocument();
  });

  it("clears the counter when the narrative fails", async () => {
    const pending = pendingNarrative();
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    await generateAndWaitForPlan();
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    await act(async () => {
      pending.failNarrative(new Error("ollama went away"));
    });

    await waitFor(() =>
      expect(screen.queryByText(/Writing mentoring guidance…/)).toBeNull(),
    );
    expect(screen.getByText("AWS")).toBeInTheDocument();
  });

  it("guards the button for the deterministic request it blocks on", async () => {
    // THE DUPLICATE-CLICK GUARD, still on the request that owns it.
    // It is deliberately NOT held through narration: making the user
    // wait for prose to press a button would reintroduce exactly the
    // coupling F2 removes.
    let settleDeterministic: (value: RoadmapResponse) => void = () => {};
    vi.mocked(getRoadmap).mockImplementation(
      () =>
        new Promise<RoadmapResponse>((resolve) => {
          settleDeterministic = resolve;
        }),
    );
    render(<RoadmapSection accessToken="token" savedJobCount={2} />);

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    const busy = await screen.findByRole("button", { name: /building/i });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    fireEvent.click(busy);
    expect(getRoadmap).toHaveBeenCalledTimes(1);

    await act(async () => {
      settleDeterministic(
        roadmap({ narrative_status: "skipped", overview: null }),
      );
    });
    // Released as soon as the schedule is on screen.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /generate roadmap/i }),
      ).toBeEnabled(),
    );
  });
});
