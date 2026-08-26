import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getJobEligibilityMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getJobEligibility: (...args: unknown[]) => getJobEligibilityMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";
import JobEligibilityPanel from "./job-eligibility-panel";

const ACCESS_TOKEN = "tok";
const JOB_ID = "job-1";

function entry(overrides: Record<string, unknown> = {}) {
  return {
    requirement_type: "cgpa" as const,
    state: "satisfied" as const,
    comparator: "gte" as const,
    requirement_numeric: "7.50",
    requirement_max: null,
    requirement_scale: "10.00",
    accepted_values: [],
    requirement_level: "required",
    candidate_numeric: "8.20",
    candidate_text: null,
    candidate_scale: "10.00",
    reason: "satisfied",
    excerpt: "Minimum CGPA 7.5",
    ...overrides,
  };
}

function eligibility(overrides: Record<string, unknown> = {}) {
  return {
    formula_version: "eligibility_v1",
    flag: "eligible" as const,
    has_requirements: true,
    has_qualification_profile: true,
    totals: {
      satisfied: 1,
      not_satisfied: 0,
      unknown: 0,
      undetermined: 0,
      total_requirements: 1,
      required_not_satisfied: 0,
    },
    requirements: [entry()],
    ...overrides,
  };
}

function renderPanel(refreshKey = 0) {
  return render(
    <JobEligibilityPanel
      accessToken={ACCESS_TOKEN}
      savedJobId={JOB_ID}
      refreshKey={refreshKey}
    />,
  );
}

beforeEach(() => {
  getJobEligibilityMock.mockReset();
});

describe("job eligibility panel", () => {
  // CASE 1
  it("says a job states no requirements rather than calling it not eligible", async () => {
    // A statement about the JOB. Neither "eligible" (a check we never
    // ran) nor "not eligible" (simply false) is honest here.
    getJobEligibilityMock.mockResolvedValue(
      eligibility({
        flag: "unknown",
        has_requirements: false,
        requirements: [],
      }),
    );
    renderPanel();

    expect(
      await screen.findByText(/No eligibility requirements detected/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Not eligible/i)).toBeNull();
  });

  // CASE 2
  it("prompts to set up a profile when the candidate has none", async () => {
    // Distinct from a profile with gaps: this one is fixed by filling
    // in a form, so point at the form instead of showing question marks.
    getJobEligibilityMock.mockResolvedValue(
      eligibility({
        flag: "unknown",
        has_qualification_profile: false,
        requirements: [
          entry({ state: "unknown", reason: "candidate_value_unknown" }),
        ],
      }),
    );
    renderPanel();

    expect(
      await screen.findByText(/Qualification profile not set up/i),
    ).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /set up eligibility/i });
    expect(link).toHaveAttribute("href", "#qualifications");
    expect(screen.queryByText(/Not eligible/i)).toBeNull();
  });

  // CASE 3
  it("evaluates a complete profile and shows each comparison", async () => {
    getJobEligibilityMock.mockResolvedValue(
      eligibility({
        requirements: [
          entry(),
          entry({
            requirement_type: "class_12_percentage",
            requirement_numeric: "80.00",
            requirement_scale: null,
            candidate_numeric: "88.00",
            candidate_scale: null,
          }),
          entry({
            requirement_type: "highest_degree",
            comparator: "in",
            accepted_values: ["btech"],
            requirement_numeric: null,
            requirement_scale: null,
            candidate_numeric: null,
            candidate_text: "btech",
            candidate_scale: null,
          }),
        ],
      }),
    );
    renderPanel();

    expect(
      await screen.findByText(/Meets all requirements/i),
    ).toBeInTheDocument();
    // The candidate's value first, then the bar it is held to.
    expect(screen.getByText("8.2 / 10 ≥ 7.5")).toBeInTheDocument();
    expect(screen.getByText("88% ≥ 80%")).toBeInTheDocument();
    // A categorical match needs no operator, and shows the display
    // name rather than the stored value.
    expect(screen.getByText("B.Tech")).toBeInTheDocument();
  });

  // CASE 4
  it("reports a missing fact as unknown, never as a failure", async () => {
    getJobEligibilityMock.mockResolvedValue(
      eligibility({
        flag: "unknown",
        totals: {
          satisfied: 1,
          not_satisfied: 0,
          unknown: 1,
          undetermined: 0,
          total_requirements: 2,
          required_not_satisfied: 0,
        },
        requirements: [
          entry(),
          entry({
            requirement_type: "graduation_year",
            state: "unknown",
            reason: "candidate_value_unknown",
            comparator: "eq",
            requirement_numeric: "2026",
            requirement_scale: null,
            candidate_numeric: null,
            candidate_scale: null,
          }),
        ],
      }),
    );
    renderPanel();

    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("8.2 / 10 ≥ 7.5")).toBeInTheDocument();
    expect(screen.getAllByText(/Not provided/i).length).toBeGreaterThan(0);
    // No invented value, and emphatically not a failure.
    expect(screen.queryByText(/Not eligible/i)).toBeNull();
  });

  it("shows a real failure as not eligible", async () => {
    getJobEligibilityMock.mockResolvedValue(
      eligibility({
        flag: "not_eligible",
        totals: {
          satisfied: 0,
          not_satisfied: 1,
          unknown: 0,
          undetermined: 0,
          total_requirements: 1,
          required_not_satisfied: 1,
        },
        requirements: [
          entry({
            state: "not_satisfied",
            reason: "below_threshold",
            requirement_numeric: "9.00",
          }),
        ],
      }),
    );
    renderPanel();

    expect(await screen.findByText("Not eligible")).toBeInTheDocument();
    expect(screen.getByText(/Below the required minimum/i)).toBeInTheDocument();
  });

  it("states each status in text, not colour alone", async () => {
    getJobEligibilityMock.mockResolvedValue(eligibility());
    renderPanel();
    expect(
      await screen.findByText(/meets this requirement/i),
    ).toBeInTheDocument();
  });

  it("refetches when the profile changes", async () => {
    getJobEligibilityMock.mockResolvedValue(eligibility());
    const { rerender } = renderPanel(0);
    await screen.findByText(/Meets all requirements/i);

    rerender(
      <JobEligibilityPanel
        accessToken={ACCESS_TOKEN}
        savedJobId={JOB_ID}
        refreshKey={1}
      />,
    );
    await waitFor(() => expect(getJobEligibilityMock).toHaveBeenCalledTimes(2));
  });

  it("surfaces an API error", async () => {
    getJobEligibilityMock.mockRejectedValue(new ApiError(403, "not yours"));
    renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent("not yours");
  });
});
