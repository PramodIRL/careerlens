import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getJobMatchMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getJobMatch: (...args: unknown[]) => getJobMatchMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";
import JobMatchPanel from "./job-match-panel";

const ACCESS_TOKEN = "tok";
const JOB_ID = "job-1";

function match(overrides: Record<string, unknown> = {}) {
  return {
    formula_version: "skill_match_v1",
    overall_score: 75,
    earned_weight: 6,
    obtainable_weight: 8,
    has_requirements: true,
    required_matched: 2,
    required_total: 2,
    by_level: {
      required: { matched: 2, total: 2 },
      preferred: { matched: 0, total: 1 },
      mentioned: { matched: 0, total: 0 },
    },
    weights: { required: 3, preferred: 2, mentioned: 1 },
    matched_skills: [
      {
        skill_id: "s1",
        skill_name: "Python",
        requirement_level: "required" as const,
        job_excerpt: "Python is required",
        candidate_status: "confirmed",
        candidate_unreviewed: false,
        candidate_evidence: [
          {
            source_type: "resume",
            excerpt: "Built backend services in Python",
            confidence: 0.9,
          },
        ],
      },
    ],
    missing_skills: [
      {
        skill_id: "s2",
        skill_name: "Docker",
        requirement_level: "preferred" as const,
        job_excerpt: "Docker is preferred",
        candidate_rejected: false,
      },
    ],
    required_missing: [],
    ...overrides,
  };
}

function renderPanel(refreshKey = 0) {
  return render(
    <JobMatchPanel
      accessToken={ACCESS_TOKEN}
      savedJobId={JOB_ID}
      refreshKey={refreshKey}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getJobMatchMock.mockResolvedValue(match());
});

describe("job match panel", () => {
  it("shows a loading state before the score arrives", () => {
    getJobMatchMock.mockReturnValue(new Promise(() => {}));
    renderPanel();
    expect(screen.getByRole("status")).toHaveTextContent(/calculating match/i);
  });

  it("surfaces an error instead of a broken score", async () => {
    getJobMatchMock.mockRejectedValue(new ApiError(500, "server exploded"));
    renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "server exploded",
    );
  });

  it("shows the score with a plain-language label, not colour alone", async () => {
    renderPanel();
    expect(await screen.findByText("75%")).toBeInTheDocument();
    expect(screen.getByText("Partial match")).toBeInTheDocument();
  });

  it("announces the score as words for assistive technology", async () => {
    renderPanel();
    expect(
      await screen.findByLabelText("75 percent match"),
    ).toBeInTheDocument();
  });

  it("shows the per-level breakdown", async () => {
    renderPanel();
    await screen.findByText("75%");
    expect(screen.getByText("Required")).toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(screen.getByText("Preferred")).toBeInTheDocument();
    expect(screen.getByText("0 / 1")).toBeInTheDocument();
  });

  it("omits a level the job does not use", async () => {
    renderPanel();
    await screen.findByText("75%");
    expect(screen.queryByText("Mentioned")).not.toBeInTheDocument();
  });

  it("lists matched and missing skills under headings", async () => {
    renderPanel();
    await screen.findByText("75%");
    expect(screen.getByText("Matched skills (1)")).toBeInTheDocument();
    expect(screen.getByText("Missing skills (1)")).toBeInTheDocument();
  });

  it("explains a matched skill with both sides of the evidence", async () => {
    renderPanel();
    await screen.findByText("75%");

    const matched = screen.getByText("Python").closest("li");
    expect(matched).not.toBeNull();
    expect(
      within(matched!).getByText(/Python is required/),
    ).toBeInTheDocument();
    expect(
      within(matched!).getByText(/Built backend services in Python/),
    ).toBeInTheDocument();
    expect(
      within(matched!).getByText(/90% match confidence/),
    ).toBeInTheDocument();
  });

  it("pairs every icon with text so meaning never depends on the glyph", async () => {
    renderPanel();
    await screen.findByText("75%");
    expect(screen.getByText("Matched:")).toBeInTheDocument();
    expect(screen.getByText("Missing:")).toBeInTheDocument();
  });

  it("flags an unreviewed match rather than presenting it as settled", async () => {
    getJobMatchMock.mockResolvedValue(
      match({
        matched_skills: [
          {
            ...match().matched_skills[0],
            candidate_status: "suggested",
            candidate_unreviewed: true,
          },
        ],
      }),
    );
    renderPanel();
    expect(await screen.findByText(/needs review/)).toBeInTheDocument();
  });

  it("says when the user rejected a missing skill", async () => {
    getJobMatchMock.mockResolvedValue(
      match({
        missing_skills: [
          { ...match().missing_skills[0], candidate_rejected: true },
        ],
      }),
    );
    renderPanel();
    expect(await screen.findByText(/you rejected this/)).toBeInTheDocument();
  });

  it("makes a missing required skill unmistakable despite the score", async () => {
    // The v1 formula applies no penalty, so the score can look healthy
    // while a hard requirement is unmet — this banner is what closes
    // that gap.
    getJobMatchMock.mockResolvedValue(
      match({
        overall_score: 77,
        required_matched: 0,
        required_total: 1,
        required_missing: [
          {
            skill_id: "s3",
            skill_name: "Kubernetes",
            requirement_level: "required" as const,
            job_excerpt: "Kubernetes is required",
            candidate_rejected: false,
          },
        ],
      }),
    );
    renderPanel();

    expect(await screen.findByText("77%")).toBeInTheDocument();
    expect(screen.getByText(/Missing 1 required skill:/)).toBeInTheDocument();
    expect(screen.getByText(/Kubernetes/)).toBeInTheDocument();
  });

  it("says no requirements were detected rather than 0% match", async () => {
    // A statement about the JOB, not about the candidate.
    getJobMatchMock.mockResolvedValue(
      match({
        has_requirements: false,
        overall_score: 0,
        obtainable_weight: 0,
        matched_skills: [],
        missing_skills: [],
      }),
    );
    renderPanel();

    expect(
      await screen.findByText(/no skill requirements detected/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("shows the formula version so the number can be reproduced", async () => {
    renderPanel();
    expect(
      await screen.findByText(/skill_match_v1: 6 of 8 weighted points/),
    ).toBeInTheDocument();
  });

  it("refetches when the refresh key changes", async () => {
    const { rerender } = renderPanel(0);
    await screen.findByText("75%");
    expect(getJobMatchMock).toHaveBeenCalledTimes(1);

    rerender(
      <JobMatchPanel
        accessToken={ACCESS_TOKEN}
        savedJobId={JOB_ID}
        refreshKey={1}
      />,
    );

    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(2));
  });

  it("does not show the loading skeleton during a background refetch", async () => {
    const { rerender } = renderPanel(0);
    await screen.findByText("75%");

    rerender(
      <JobMatchPanel
        accessToken={ACCESS_TOKEN}
        savedJobId={JOB_ID}
        refreshKey={1}
      />,
    );

    expect(screen.queryByText(/calculating match/i)).not.toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
  });
});
