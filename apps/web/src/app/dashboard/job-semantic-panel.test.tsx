import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getJobSemanticMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getJobSemantic: (...args: unknown[]) => getJobSemanticMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";
import JobSemanticPanel from "./job-semantic-panel";

const ACCESS_TOKEN = "tok";
const JOB_ID = "job-1";

function semantic(overrides: Record<string, unknown> = {}) {
  return {
    formula_version: "semantic_fit_v1",
    fit: 15,
    band: "strong",
    model_identifier: "sentence-transformers/all-MiniLM-L6-v2",
    considered: 2,
    evidence: [
      {
        embedding_id: "e1",
        source_type: "skill_evidence",
        source_id: "ev1",
        evidence_id: "ev1",
        excerpt: "Built Kubernetes-based microservices",
        evidence_source_type: "resume",
        evidence_source_identifier: "resume-1",
        similarity: 0.4556,
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  getJobSemanticMock.mockReset();
});

describe("JobSemanticPanel", () => {
  it("shows the band, the excerpt and its similarity", async () => {
    getJobSemanticMock.mockResolvedValue(semantic());

    render(<JobSemanticPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />);

    expect(
      await screen.findByText("Built Kubernetes-based microservices"),
    ).toBeInTheDocument();
    expect(screen.getByText("Strong")).toBeInTheDocument();
    expect(screen.getByText(/Resume · similarity 0\.46/)).toBeInTheDocument();
  });

  it("never claims the candidate has a skill", async () => {
    /* The load-bearing assertion of this panel. Semantic relevance is
       supporting evidence; asserting skill ownership from it is exactly
       what the product rule forbids. */
    getJobSemanticMock.mockResolvedValue(semantic());

    render(<JobSemanticPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />);

    expect(
      await screen.findByText(/does not confirm the candidate has a skill/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\bhas skill\b/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/\bmatched\b/i)).not.toBeInTheDocument();
  });

  it("says nothing was found rather than showing a low score", async () => {
    getJobSemanticMock.mockResolvedValue(
      semantic({ fit: 0, band: "none", evidence: [] }),
    );

    render(<JobSemanticPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />);

    expect(
      await screen.findByText("No closely related evidence found."),
    ).toBeInTheDocument();
  });

  it("explains an unindexed job instead of rendering an empty band", async () => {
    getJobSemanticMock.mockResolvedValue(
      semantic({ considered: 0, fit: 0, band: "none", evidence: [] }),
    );

    render(<JobSemanticPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />);

    expect(
      await screen.findByText(/has not been indexed for related evidence/i),
    ).toBeInTheDocument();
  });

  it("fails quietly, because the deterministic match is unaffected", async () => {
    getJobSemanticMock.mockRejectedValue(new ApiError(500, "boom"));

    render(<JobSemanticPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />);

    await waitFor(() =>
      expect(
        screen.getByText("Related evidence is unavailable right now."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
