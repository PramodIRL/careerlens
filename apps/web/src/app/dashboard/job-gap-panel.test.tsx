import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getJobGapsMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getJobGaps: (...args: unknown[]) => getJobGapsMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";
import JobGapPanel from "./job-gap-panel";

const ACCESS_TOKEN = "tok";
const JOB_ID = "job-1";

function entry(overrides: Record<string, unknown> = {}) {
  return {
    skill_id: "s1",
    skill_name: "Kubernetes",
    requirement_level: "required" as const,
    job_excerpt: "Kubernetes is required for deployment",
    candidate_status: null,
    candidate_evidence: [],
    ...overrides,
  };
}

function gaps(overrides: Record<string, unknown> = {}) {
  const base = {
    formula_version: "skill_gap_v1",
    required_gaps: [],
    preferred_gaps: [],
    informational_gaps: [],
    needs_confirmation: [],
    rejected_requirements: [],
    totals: {
      required_gaps: 0,
      preferred_gaps: 0,
      informational_gaps: 0,
      needs_confirmation: 0,
      rejected_requirements: 0,
      satisfied: 2,
      total_requirements: 2,
    },
    ...overrides,
  };
  return base;
}

function renderPanel(refreshKey = 0) {
  return render(
    <JobGapPanel
      accessToken={ACCESS_TOKEN}
      savedJobId={JOB_ID}
      refreshKey={refreshKey}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getJobGapsMock.mockResolvedValue(gaps());
});

describe("job gap panel", () => {
  it("shows a loading state", () => {
    getJobGapsMock.mockReturnValue(new Promise(() => {}));
    renderPanel();
    expect(screen.getByRole("status")).toHaveTextContent(
      /checking skill gaps/i,
    );
  });

  it("surfaces an error instead of a blank panel", async () => {
    getJobGapsMock.mockRejectedValue(new ApiError(500, "server exploded"));
    renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "server exploded",
    );
  });

  it("says there are no gaps when everything is satisfied", async () => {
    renderPanel();
    expect(
      await screen.findByText(/no gaps — you match every requirement/i),
    ).toBeInTheDocument();
  });

  it("distinguishes a job with no requirements from a perfect match", async () => {
    // A fact about the JOB, not a compliment to the candidate.
    getJobGapsMock.mockResolvedValue(
      gaps({
        totals: {
          required_gaps: 0,
          preferred_gaps: 0,
          informational_gaps: 0,
          needs_confirmation: 0,
          rejected_requirements: 0,
          satisfied: 0,
          total_requirements: 0,
        },
      }),
    );
    renderPanel();

    expect(
      await screen.findByText(/no skill requirements detected/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/you match every requirement/i),
    ).not.toBeInTheDocument();
  });

  it("renders a required gap with its job excerpt", async () => {
    getJobGapsMock.mockResolvedValue(gaps({ required_gaps: [entry()] }));
    renderPanel();

    expect(await screen.findByText("Required gaps (1)")).toBeInTheDocument();
    const row = screen.getByText("Kubernetes").closest("li");
    expect(
      within(row!).getByText(/Kubernetes is required for deployment/),
    ).toBeInTheDocument();
  });

  it("says no evidence was found rather than storing that sentence", async () => {
    getJobGapsMock.mockResolvedValue(gaps({ required_gaps: [entry()] }));
    renderPanel();
    await screen.findByText("Required gaps (1)");

    expect(screen.getByText(/No evidence found/)).toBeInTheDocument();
    expect(
      screen.getByText(/We found no evidence for this skill on your profile/),
    ).toBeInTheDocument();
  });

  it("renders needs-confirmation with the candidate evidence", async () => {
    getJobGapsMock.mockResolvedValue(
      gaps({
        needs_confirmation: [
          entry({
            skill_id: "s2",
            skill_name: "Python",
            candidate_status: "suggested",
            candidate_evidence: [
              {
                source_type: "resume",
                excerpt: "Built backend services in Python",
                confidence: 0.9,
              },
            ],
          }),
        ],
      }),
    );
    renderPanel();

    expect(
      await screen.findByText("Needs confirmation (1)"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Found, but not yet reviewed by you/),
    ).toBeInTheDocument();
    const row = screen.getByText("Python").closest("li");
    expect(
      within(row!).getByText(/Built backend services in Python/),
    ).toBeInTheDocument();
  });

  it("renders a rejected requirement distinctly from a missing one", async () => {
    // "You marked this as not yours" and "we found nothing" are
    // different claims and must read differently.
    getJobGapsMock.mockResolvedValue(
      gaps({
        rejected_requirements: [
          entry({
            skill_id: "s3",
            skill_name: "Docker",
            requirement_level: "preferred" as const,
            candidate_status: "rejected",
            job_excerpt: "Docker experience is preferred",
            candidate_evidence: [
              {
                source_type: "resume",
                excerpt: "Used Docker daily",
                confidence: 0.9,
              },
            ],
          }),
        ],
      }),
    );
    renderPanel();

    expect(
      await screen.findByText("You rejected these (1)"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/You marked this as not yours/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No evidence found/)).not.toBeInTheDocument();
    const row = screen.getByText("Docker").closest("li");
    expect(within(row!).getByText(/Used Docker daily/)).toBeInTheDocument();
  });

  it("renders preferred and informational buckets with their labels", async () => {
    getJobGapsMock.mockResolvedValue(
      gaps({
        preferred_gaps: [
          entry({
            skill_id: "s4",
            skill_name: "Docker",
            requirement_level: "preferred" as const,
          }),
        ],
        informational_gaps: [
          entry({
            skill_id: "s5",
            skill_name: "Redis",
            requirement_level: "mentioned" as const,
          }),
        ],
      }),
    );
    renderPanel();

    expect(await screen.findByText("Preferred gaps (1)")).toBeInTheDocument();
    expect(screen.getByText("Informational (1)")).toBeInTheDocument();
    expect(screen.getByText(/· Preferred ·/)).toBeInTheDocument();
    expect(screen.getByText(/· Mentioned ·/)).toBeInTheDocument();
  });

  it("orders the sections required, needs-confirmation, preferred, informational, rejected", async () => {
    getJobGapsMock.mockResolvedValue(
      gaps({
        required_gaps: [entry({ skill_id: "a", skill_name: "Kubernetes" })],
        needs_confirmation: [entry({ skill_id: "b", skill_name: "Python" })],
        preferred_gaps: [entry({ skill_id: "c", skill_name: "Docker" })],
        informational_gaps: [entry({ skill_id: "d", skill_name: "Redis" })],
        rejected_requirements: [entry({ skill_id: "e", skill_name: "Java" })],
      }),
    );
    renderPanel();
    await screen.findByText("Required gaps (1)");

    const headings = screen
      .getAllByRole("heading", { level: 5 })
      .map((h) => h.textContent);
    expect(headings).toEqual([
      "Required gaps (1)",
      "Needs confirmation (1)",
      "Preferred gaps (1)",
      "Informational (1)",
      "You rejected these (1)",
    ]);
  });

  it("uses semantic headings for each section", async () => {
    getJobGapsMock.mockResolvedValue(gaps({ required_gaps: [entry()] }));
    renderPanel();

    expect(
      await screen.findByRole("heading", {
        level: 5,
        name: "Required gaps (1)",
      }),
    ).toBeInTheDocument();
  });

  it("shows the satisfied count and formula version", async () => {
    getJobGapsMock.mockResolvedValue(gaps({ required_gaps: [entry()] }));
    renderPanel();

    expect(
      await screen.findByText(/2 of 2 requirements satisfied · skill_gap_v1/),
    ).toBeInTheDocument();
  });

  it("refetches when the refresh key changes", async () => {
    const { rerender } = renderPanel(0);
    await screen.findByText(/no gaps/i);
    expect(getJobGapsMock).toHaveBeenCalledTimes(1);

    rerender(
      <JobGapPanel
        accessToken={ACCESS_TOKEN}
        savedJobId={JOB_ID}
        refreshKey={1}
      />,
    );

    await waitFor(() => expect(getJobGapsMock).toHaveBeenCalledTimes(2));
  });

  it("does not show the loading skeleton during a background refetch", async () => {
    getJobGapsMock.mockResolvedValue(gaps({ required_gaps: [entry()] }));
    const { rerender } = renderPanel(0);
    await screen.findByText("Required gaps (1)");

    rerender(
      <JobGapPanel
        accessToken={ACCESS_TOKEN}
        savedJobId={JOB_ID}
        refreshKey={1}
      />,
    );

    expect(screen.queryByText(/checking skill gaps/i)).not.toBeInTheDocument();
    expect(screen.getByText("Required gaps (1)")).toBeInTheDocument();
  });
});
