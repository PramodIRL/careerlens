import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getJobExplanationMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getJobExplanation: (...args: unknown[]) => getJobExplanationMock(...args),
  };
});

import JobExplanationPanel from "./job-explanation-panel";

const ACCESS_TOKEN = "tok";
const JOB_ID = "job-1";

function explanation(overrides: Record<string, unknown> = {}) {
  return {
    status: "generated",
    reason: null,
    schema_version: "match_explanation_v1",
    provider: "mock",
    match_formula_version: "skill_match_v1",
    overall_score: 75,
    has_requirements: true,
    gap_formula_version: "skill_gap_v1",
    semantic_formula_version: "semantic_fit_v1",
    summary: "Acme — Engineer: skill_match_v1 scored this match 75.",
    strengths: [
      {
        text: "You already have evidence for Python and PostgreSQL.",
        evidence_ids: ["ev1", "ev2"],
      },
    ],
    gaps: [{ text: "Docker is preferred here.", evidence_ids: [] }],
    next_steps: ["Look at what Docker would involve."],
    cited_evidence: [
      {
        evidence_id: "ev1",
        source_type: "resume",
        source_identifier: "resume-1",
        excerpt: "Built backend services in Python",
      },
      {
        evidence_id: "ev2",
        source_type: "github",
        source_identifier: "octocat/api",
        excerpt: "Postgres-backed service",
      },
    ],
    ...overrides,
  };
}

describe("JobExplanationPanel", () => {
  beforeEach(() => {
    getJobExplanationMock.mockReset();
  });

  it("requests nothing until the user asks", () => {
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    expect(getJobExplanationMock).not.toHaveBeenCalled();
  });

  it("leads with readable prose, not a list of evidence rows", async () => {
    getJobExplanationMock.mockResolvedValue(explanation());
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));

    await waitFor(() =>
      expect(
        screen.getByText(
          "You already have evidence for Python and PostgreSQL.",
        ),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Look at what Docker would involve."),
    ).toBeInTheDocument();
  });

  it("puts every cited row behind one supporting-evidence disclosure", async () => {
    getJobExplanationMock.mockResolvedValue(explanation());
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));

    await waitFor(() =>
      expect(screen.getByText("Supporting evidence (2)")).toBeInTheDocument(),
    );
    // Still there and still the candidate's own stored rows — just not
    // interleaved through the prose, and listed once each.
    expect(
      screen.getByText(/Built backend services in Python/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Postgres-backed service/)).toBeInTheDocument();
  });

  it("says why a rejected explanation is not shown, and shows none of it", async () => {
    getJobExplanationMock.mockResolvedValue(
      explanation({
        status: "rejected",
        reason: "unknown_evidence_id",
        summary: null,
        strengths: [],
        gaps: [],
        next_steps: [],
        cited_evidence: [],
      }),
    );
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));

    await waitFor(() =>
      expect(
        screen.getByText(/cited evidence that does not exist/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Strengths")).not.toBeInTheDocument();
    expect(screen.queryByText("Next steps")).not.toBeInTheDocument();
  });
});
