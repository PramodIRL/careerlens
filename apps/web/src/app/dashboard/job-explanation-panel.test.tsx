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
      { text: "Your stored evidence covers Python.", evidence_ids: ["ev1"] },
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

  it("shows each claim beside the stored evidence it cites", async () => {
    getJobExplanationMock.mockResolvedValue(explanation());
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));

    await waitFor(() =>
      expect(
        screen.getByText("Your stored evidence covers Python."),
      ).toBeInTheDocument(),
    );
    // The quote is the candidate's own stored row, not model prose.
    expect(
      screen.getByText(/Built backend services in Python/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Look at what Docker would involve."),
    ).toBeInTheDocument();
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
