import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

// =====================================================================
// 7.2 F3 — elapsed-time feedback while a local model generates
//
// `explanation_timeout_seconds` is 180 and a local 7B model generates
// at roughly ten tokens a second, so "Writing an explanation…" could
// sit unchanged for minutes with no way to tell a working model from a
// daemon that had gone away. The counter is the difference.
//
// FAKE TIMERS, because the assertion is about the passage of time and a
// real one would make this test take as long as the thing it measures.
// =====================================================================

describe("elapsed-time feedback while generating", () => {
  beforeEach(() => {
    getJobExplanationMock.mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Hold the request open so the loading state can be inspected. */
  function pendingExplanation() {
    let settle: (value: unknown) => void = () => {};
    getJobExplanationMock.mockReturnValue(
      new Promise((resolve) => {
        settle = resolve;
      }),
    );
    return { settle: (value: unknown) => settle(value) };
  }

  it("counts the seconds while a generation is in flight", async () => {
    pendingExplanation();
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      /Writing an explanation… 0s/,
    );

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      /Writing an explanation… 3s/,
    );
  });

  it("explains the wait once it stops being unremarkable", async () => {
    pendingExplanation();
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    await screen.findByRole("status");

    // Nothing at five seconds: a note that fires on every normal run is
    // a note the user learns to ignore.
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.queryByText(/runs on a local model/i)).toBeNull();

    await act(async () => {
      vi.advanceTimersByTime(16000);
    });
    // LEADS WITH WHAT IS STILL TRUE: the deterministic results above are
    // complete and are not waiting for this.
    expect(screen.getByText(/runs on a local model/i)).toBeInTheDocument();
    expect(
      screen.getByText(/scores and gaps are already complete/i),
    ).toBeInTheDocument();
  });

  it("stops and clears the counter when the explanation arrives", async () => {
    const pending = pendingExplanation();
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    await screen.findByRole("status");
    await act(async () => {
      vi.advanceTimersByTime(4000);
    });

    await act(async () => {
      pending.settle(explanation());
    });

    await waitFor(() =>
      expect(screen.queryByText(/Writing an explanation…/)).toBeNull(),
    );
    // And it does not keep counting against a finished request.
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.queryByText(/Writing an explanation…/)).toBeNull();
  });

  it("stops the counter when the generation fails", async () => {
    // A FAILURE ENDS IT IDENTICALLY. The caller clears its loading flag
    // in a `finally`, and a stale count left under an error message
    // would read as though something were still running.
    getJobExplanationMock.mockRejectedValue(new Error("ollama went away"));
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/Writing an explanation…/)).toBeNull();
  });

  it("restarts from zero on a regenerate", async () => {
    const first = pendingExplanation();
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    await screen.findByRole("status");
    await act(async () => {
      vi.advanceTimersByTime(7000);
    });
    await act(async () => {
      first.settle(explanation());
    });
    await screen.findByRole("button", { name: "Regenerate" });

    pendingExplanation();
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));

    // ZERO, not seven: the previous run's number must not be inherited.
    await waitFor(() =>
      expect(
        screen.getByText(/Writing an explanation… 0s/),
      ).toBeInTheDocument(),
    );
  });

  it("keeps the button disabled while one generation is running", async () => {
    // The existing duplicate-click guard, unchanged by the counter.
    pendingExplanation();
    render(
      <JobExplanationPanel accessToken={ACCESS_TOKEN} savedJobId={JOB_ID} />,
    );

    const button = screen.getByRole("button", { name: "Explain" });
    fireEvent.click(button);
    await screen.findByRole("status");

    expect(button).toBeDisabled();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(getJobExplanationMock).toHaveBeenCalledTimes(1);
  });
});
