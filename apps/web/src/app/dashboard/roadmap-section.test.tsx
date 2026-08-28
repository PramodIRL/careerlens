import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
    vi.mocked(listSavedJobs).mockResolvedValue([
      { id: "a" },
      { id: "b" },
    ] as never);
    vi.mocked(getRoadmap).mockResolvedValue(roadmap());
    render(<RoadmapSection accessToken="token" />);

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

  /** Four jobs, then two — the deletion the bug report describes. */
  function shrinkingCollection() {
    vi.mocked(listSavedJobs)
      .mockResolvedValueOnce([
        { id: "a" },
        { id: "b" },
        { id: "c" },
        { id: "d" },
      ] as never)
      .mockResolvedValue([{ id: "a" }, { id: "b" }] as never);
  }

  it("re-reads the count when a job is deleted", async () => {
    shrinkingCollection();
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
    );
    expect(await screen.findByText(/of 4 saved jobs/)).toBeInTheDocument();

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);

    expect(await screen.findByText(/of 2 saved jobs/)).toBeInTheDocument();
  });

  it("clamps the chosen job count down to what is left", async () => {
    shrinkingCollection();
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
    );
    const input = await screen.findByLabelText(/jobs to prepare for/i);
    await waitFor(() => expect(input).toHaveValue(4));

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);

    await waitFor(() => expect(input).toHaveValue(2));
    expect(input).toHaveAttribute("max", "2");
  });

  it("never asks for more jobs than the user still has", async () => {
    // THE 422 ITSELF. Before the fix this sent top_n=4 against two saved
    // jobs, the API refused it, and the section could not recover.
    shrinkingCollection();
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText(/jobs to prepare for/i)).toHaveValue(4),
    );

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);
    await waitFor(() =>
      expect(screen.getByLabelText(/jobs to prepare for/i)).toHaveValue(2),
    );
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));

    await waitFor(() =>
      expect(getRoadmap).toHaveBeenCalledWith("token", {
        topN: 2,
        durationDays: 28,
        hoursPerDay: 1,
      }),
    );
  });

  it("withdraws a plan built from jobs that have since changed", async () => {
    vi.mocked(listSavedJobs).mockResolvedValue([
      { id: "a" },
      { id: "b" },
    ] as never);
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
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
    vi.mocked(listSavedJobs).mockResolvedValue([
      { id: "a" },
      { id: "b" },
    ] as never);
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
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
    vi.mocked(listSavedJobs).mockResolvedValue([{ id: "a" }] as never);
    render(<RoadmapSection accessToken="token" jobsVersion={3} />);

    expect(await screen.findByText(/of 1 saved job/)).toBeInTheDocument();
    expect(
      screen.queryByText(/saved jobs changed after this plan was made/i),
    ).toBeNull();
  });

  it("counts once per change, not once per render", async () => {
    // THE PERFORMANCE GUARD. `jobsVersion` is a dependency, so a render
    // that changes nothing must not re-read the collection.
    vi.mocked(listSavedJobs).mockResolvedValue([{ id: "a" }] as never);
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
    );
    await waitFor(() => expect(listSavedJobs).toHaveBeenCalledTimes(1));

    rerender(<RoadmapSection accessToken="token" jobsVersion={0} />);
    rerender(<RoadmapSection accessToken="token" jobsVersion={0} />);
    expect(listSavedJobs).toHaveBeenCalledTimes(1);

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);
    await waitFor(() => expect(listSavedJobs).toHaveBeenCalledTimes(2));
  });

  it("re-reads the count when a job is added", async () => {
    vi.mocked(listSavedJobs)
      .mockResolvedValueOnce([{ id: "a" }] as never)
      .mockResolvedValue([{ id: "a" }, { id: "b" }] as never);
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
    );
    expect(await screen.findByText(/of 1 saved job/)).toBeInTheDocument();

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);

    expect(await screen.findByText(/of 2 saved jobs/)).toBeInTheDocument();
  });

  it("retires a plan after a reorder, whose priorities have moved", async () => {
    // The count is identical; `roadmap_priority_v1` reads the ORDER, so
    // the ranks in a plan drawn before the move are no longer the
    // user's.
    vi.mocked(listSavedJobs).mockResolvedValue([
      { id: "a" },
      { id: "b" },
    ] as never);
    vi.mocked(getRoadmap).mockResolvedValue(roadmap({ saved_job_count: 2 }));
    const { rerender } = render(
      <RoadmapSection accessToken="token" jobsVersion={0} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
    expect(await screen.findByText("AWS")).toBeInTheDocument();

    rerender(<RoadmapSection accessToken="token" jobsVersion={1} />);

    await waitFor(() => expect(screen.queryByText("AWS")).toBeNull());
    expect(screen.getByRole("status")).toHaveTextContent(
      /generate the roadmap again/i,
    );
  });
});
