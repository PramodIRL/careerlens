import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getSkillProfileMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getSkillProfile: (...args: unknown[]) => getSkillProfileMock(...args),
  };
});

import SkillProfileSection from "./skill-profile-section";

const ACCESS_TOKEN = "tok";

function profile(summaryOverrides: Record<string, unknown> = {}) {
  return {
    summary: {
      total: 3,
      confirmed: 2,
      suggested: 1,
      rejected: 0,
      by_source: { resume: 2, github: 3, manual: 1 },
      multi_source: 2,
      reviewed: false,
      ...summaryOverrides,
    },
    skills: [],
  };
}

describe("skill profile section", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading state before the profile arrives", () => {
    getSkillProfileMock.mockReturnValue(new Promise(() => {}));
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      /loading your skill profile/i,
    );
  });

  it("renders the headline counts", async () => {
    getSkillProfileMock.mockResolvedValue(profile());
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(screen.getByText("Skills")).toBeInTheDocument();
    });
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
  });

  it("breaks evidence down by source", async () => {
    getSkillProfileMock.mockResolvedValue(profile());
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(screen.getByText("Resume")).toBeInTheDocument();
    });
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.getByText("Added by you")).toBeInTheDocument();
    // The by_source figures, not the headline counts.
    expect(
      screen.getByText("Where your evidence comes from"),
    ).toBeInTheDocument();
  });

  it("highlights corroborated skills when there are any", async () => {
    getSkillProfileMock.mockResolvedValue(profile({ multi_source: 2 }));
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(
        screen.getByText(/backed by more than one source/i),
      ).toBeInTheDocument();
    });
  });

  it("says nothing about corroboration when no skill is multi-source", async () => {
    getSkillProfileMock.mockResolvedValue(profile({ multi_source: 0 }));
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(screen.getByText("Skills")).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/backed by more than one source/i),
    ).not.toBeInTheDocument();
  });

  it("notes rejected skills are not shown, without listing them", async () => {
    getSkillProfileMock.mockResolvedValue(profile({ rejected: 2 }));
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(
        screen.getByText(/2 rejected skills not shown/i),
      ).toBeInTheDocument();
    });
  });

  it("prompts for input when there is no evidence at all", async () => {
    getSkillProfileMock.mockResolvedValue(
      profile({
        total: 0,
        confirmed: 0,
        suggested: 0,
        rejected: 0,
        by_source: { resume: 0, github: 0, manual: 0 },
        multi_source: 0,
        reviewed: true,
      }),
    );
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(screen.getByText(/no evidence yet/i)).toBeInTheDocument();
    });
  });

  it("surfaces an error instead of a broken summary", async () => {
    getSkillProfileMock.mockRejectedValue(new Error("boom"));
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
  });

  it("does not list individual skills — that is the review section's job", async () => {
    getSkillProfileMock.mockResolvedValue({
      ...profile(),
      skills: [
        {
          id: "cs-1",
          skill_id: "s-1",
          skill_name: "Python",
          skill_category: "language",
          status: "confirmed",
          sources: ["resume", "github"],
          evidence_count: 2,
          strongest_evidence_confidence: 0.9,
          evidence: [],
        },
      ],
    });
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} />);

    await waitFor(() => {
      expect(screen.getByText("Skills")).toBeInTheDocument();
    });
    // Two lists of the same skill names on one page would be a
    // duplication smell; this section deliberately shows only the rollup.
    expect(screen.queryByText("Python")).not.toBeInTheDocument();
  });
});

describe("skill profile auto-refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not fire the settle refetch on first mount", async () => {
    // Nothing is in flight on a cold mount, so a second fetch is waste.
    getSkillProfileMock.mockResolvedValue(profile());
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={0} />);
    await waitFor(() => expect(screen.getByText("Skills")).toBeInTheDocument());

    expect(getSkillProfileMock).toHaveBeenCalledTimes(1);
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(getSkillProfileMock).toHaveBeenCalledTimes(1);
  }, 10000);

  it("refetches immediately and once more when refreshKey changes", async () => {
    // The workers commit a job's terminal status BEFORE the evidence
    // derived from it, so the immediate fetch can lose the race. The
    // single delayed follow-up covers that window.
    getSkillProfileMock.mockResolvedValue(profile());
    const { rerender } = render(
      <SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={0} />,
    );
    await waitFor(() => expect(screen.getByText("Skills")).toBeInTheDocument());
    getSkillProfileMock.mockClear();

    rerender(<SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={1} />);

    await waitFor(() => expect(getSkillProfileMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getSkillProfileMock).toHaveBeenCalledTimes(2), {
      timeout: 4000,
    });

    // Exactly two — the follow-up is a single retry, not a loop.
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(getSkillProfileMock).toHaveBeenCalledTimes(2);
  }, 15000);

  it("cancels the pending settle refetch on unmount", async () => {
    getSkillProfileMock.mockResolvedValue(profile());
    const { rerender, unmount } = render(
      <SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={0} />,
    );
    await waitFor(() => expect(screen.getByText("Skills")).toBeInTheDocument());

    rerender(<SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={1} />);
    await waitFor(() => expect(getSkillProfileMock).toHaveBeenCalledTimes(2));
    unmount();

    getSkillProfileMock.mockClear();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(getSkillProfileMock).not.toHaveBeenCalled();
  }, 10000);

  it("does not show the loading skeleton during a background refetch", async () => {
    getSkillProfileMock.mockResolvedValue(profile());
    const { rerender } = render(
      <SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={0} />,
    );
    await waitFor(() => expect(screen.getByText("Skills")).toBeInTheDocument());

    rerender(<SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={1} />);

    expect(
      screen.queryByText(/Loading your skill profile/i),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Skills")).toBeInTheDocument();
  }, 10000);

  it("keeps the last good summary when a background refetch fails", async () => {
    getSkillProfileMock
      .mockResolvedValueOnce(profile())
      .mockRejectedValue(new Error("network"));
    const { rerender } = render(
      <SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={0} />,
    );
    await waitFor(() => expect(screen.getByText("Skills")).toBeInTheDocument());

    rerender(<SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={1} />);
    await waitFor(() => expect(getSkillProfileMock).toHaveBeenCalledTimes(2));

    expect(screen.getByText("Skills")).toBeInTheDocument();
  }, 10000);

  it("keeps the manual Refresh button working", async () => {
    getSkillProfileMock.mockResolvedValue(profile());
    render(<SkillProfileSection accessToken={ACCESS_TOKEN} refreshKey={0} />);
    await waitFor(() => expect(screen.getByText("Skills")).toBeInTheDocument());
    getSkillProfileMock.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(getSkillProfileMock).toHaveBeenCalledTimes(1));
  }, 10000);
});
