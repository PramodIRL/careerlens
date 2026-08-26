import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getQualificationsMock = vi.fn();
const updateQualificationsMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getQualifications: (...args: unknown[]) => getQualificationsMock(...args),
    updateQualifications: (...args: unknown[]) =>
      updateQualificationsMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";
import QualificationsSection from "./qualifications-section";

const ACCESS_TOKEN = "tok";

/** A fact the candidate has asserted. Only confirmed facts are
 * authoritative for eligibility, so this is what a real profile looks
 * like. */
function declaredFact(overrides: Record<string, unknown> = {}) {
  return {
    status: "confirmed",
    source_type: "manual",
    source_identifier: null,
    excerpt: null,
    extraction_method: "manual_entry",
    confidence: null,
    ...overrides,
  };
}

function empty(overrides: Record<string, unknown> = {}) {
  return {
    cgpa: null,
    cgpa_scale: null,
    class_10_percentage: null,
    class_12_percentage: null,
    highest_degree: null,
    field_of_study: null,
    graduation_year: null,
    years_experience: null,
    updated_at: null,
    facts: {},
    ...overrides,
  };
}

/** A complete, declared profile. */
function declared(overrides: Record<string, unknown> = {}) {
  return empty({
    cgpa: "8.20",
    cgpa_scale: null,
    class_10_percentage: "91.00",
    class_12_percentage: "88.00",
    highest_degree: "btech",
    field_of_study: "computer_science",
    graduation_year: 2026,
    years_experience: "0.00",
    facts: {
      cgpa: declaredFact(),
      class_10_percentage: declaredFact(),
      class_12_percentage: declaredFact(),
      highest_degree: declaredFact(),
      field_of_study: declaredFact(),
      graduation_year: declaredFact(),
      years_experience: declaredFact(),
    },
    ...overrides,
  });
}

beforeEach(() => {
  getQualificationsMock.mockReset();
  updateQualificationsMock.mockReset();
});

describe("qualifications section", () => {
  it("shows the declared profile", async () => {
    getQualificationsMock.mockResolvedValue(declared());
    render(<QualificationsSection accessToken={ACCESS_TOKEN} />);

    // The scale is shown even when unstated, because 10 is what the
    // comparison actually uses.
    expect(await screen.findByText("8.2 / 10")).toBeInTheDocument();
    expect(screen.getByText("91%")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("B.Tech")).toBeInTheDocument();
    expect(screen.getByText("Computer Science")).toBeInTheDocument();
    expect(screen.getByText("2026")).toBeInTheDocument();
    expect(screen.getByText("0 years")).toBeInTheDocument();
  });

  it("shows an explicit scale rather than the default", async () => {
    getQualificationsMock.mockResolvedValue(
      declared({ cgpa: "3.60", cgpa_scale: "4.00" }),
    );
    render(<QualificationsSection accessToken={ACCESS_TOKEN} />);
    expect(await screen.findByText("3.6 / 4")).toBeInTheDocument();
  });

  it("says a missing fact is not provided, never demanding it", async () => {
    // Never a blocker, never a failure — the user can start using
    // CareerLens without completing anything.
    getQualificationsMock.mockResolvedValue(empty());
    render(<QualificationsSection accessToken={ACCESS_TOKEN} />);

    expect(
      (await screen.findAllByText(/Not provided/i)).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/have not added any qualifications/i),
    ).toBeInTheDocument();
  });

  it("distinguishes zero experience from a missing fact", async () => {
    // A declared zero is a real value. It must not render the same way as
    // "we do not know".
    getQualificationsMock.mockResolvedValue(declared());
    render(<QualificationsSection accessToken={ACCESS_TOKEN} />);

    const experience = (await screen.findByText("Experience")).closest("li");
    expect(experience).toHaveTextContent("0 years");
    expect(experience).not.toHaveTextContent(/Not provided/i);
  });

  it("saves a correction and notifies the dashboard", async () => {
    getQualificationsMock.mockResolvedValue(declared());
    updateQualificationsMock.mockResolvedValue(declared({ cgpa: "9.10" }));
    const onChanged = vi.fn();
    render(
      <QualificationsSection
        accessToken={ACCESS_TOKEN}
        onChanged={onChanged}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: /review \/ edit/i }),
    );
    fireEvent.change(screen.getByLabelText("CGPA"), {
      target: { value: "9.1" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /save qualifications/i }),
    );

    await waitFor(() => expect(updateQualificationsMock).toHaveBeenCalled());
    expect(updateQualificationsMock.mock.calls[0][1]).toMatchObject({
      cgpa: "9.1",
    });
    // One edit changes the eligibility answer for every saved job.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("refetches when the refresh key changes", async () => {
    getQualificationsMock.mockResolvedValue(empty());
    const { rerender } = render(
      <QualificationsSection accessToken={ACCESS_TOKEN} refreshKey={0} />,
    );
    await screen.findAllByText(/Not provided/i);

    getQualificationsMock.mockResolvedValue(declared());
    rerender(
      <QualificationsSection accessToken={ACCESS_TOKEN} refreshKey={1} />,
    );

    expect(await screen.findByText("8.2 / 10")).toBeInTheDocument();
  });

  it("surfaces a load error", async () => {
    getQualificationsMock.mockRejectedValue(new ApiError(500, "boom"));
    render(<QualificationsSection accessToken={ACCESS_TOKEN} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
