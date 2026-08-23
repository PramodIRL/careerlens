import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const listCandidateSkillsMock = vi.fn();
const addCandidateSkillMock = vi.fn();
const updateCandidateSkillStatusMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    listCandidateSkills: (...args: unknown[]) =>
      listCandidateSkillsMock(...args),
    addCandidateSkill: (...args: unknown[]) => addCandidateSkillMock(...args),
    updateCandidateSkillStatus: (...args: unknown[]) =>
      updateCandidateSkillStatusMock(...args),
  };
});

import SkillsSection from "./skills-section";

const ACCESS_TOKEN = "tok";

function candidateSkill(overrides: Record<string, unknown> = {}) {
  return {
    id: "cs-1",
    skill_id: "skill-1",
    skill_name: "Python",
    skill_category: "language",
    status: "suggested" as const,
    evidence: [
      {
        id: "ev-1",
        source_type: "resume" as const,
        source_identifier: "resume-1",
        excerpt: "Built a data pipeline in Python for a research team.",
        extraction_method: "resume_alias_match",
        confidence: 0.9,
        created_at: "2026-01-01T00:00:00Z",
      },
    ],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderSection() {
  return render(<SkillsSection accessToken={ACCESS_TOKEN} />);
}

beforeEach(() => {
  listCandidateSkillsMock.mockReset();
  addCandidateSkillMock.mockReset();
  updateCandidateSkillStatusMock.mockReset();
});

describe("skills section", () => {
  it("shows an empty state when there are no skills", async () => {
    listCandidateSkillsMock.mockResolvedValue([]);

    renderSection();

    expect(await screen.findByText(/no skills yet/i)).toBeInTheDocument();
  });

  it("lists a suggested skill with its evidence excerpt", async () => {
    listCandidateSkillsMock.mockResolvedValue([candidateSkill()]);

    renderSection();

    expect(await screen.findByText("Python")).toBeInTheDocument();
    expect(screen.getByText(/needs review/i)).toBeInTheDocument();
    expect(screen.getByText(/from your resume/i)).toBeInTheDocument();
    expect(screen.getByText(/90% confidence/)).toBeInTheDocument();
    expect(
      screen.getByText("Built a data pipeline in Python for a research team."),
    ).toBeInTheDocument();
    expect(listCandidateSkillsMock).toHaveBeenCalledWith(ACCESS_TOKEN);
  });

  it("groups skills by review status", async () => {
    listCandidateSkillsMock.mockResolvedValue([
      candidateSkill(),
      candidateSkill({ id: "cs-2", skill_name: "Docker", status: "confirmed" }),
      candidateSkill({ id: "cs-3", skill_name: "Go", status: "rejected" }),
    ]);

    renderSection();

    expect(await screen.findByText(/needs review \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText(/confirmed \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText(/rejected \(1\)/i)).toBeInTheDocument();
  });

  it("confirms a suggested skill", async () => {
    listCandidateSkillsMock.mockResolvedValue([candidateSkill()]);
    updateCandidateSkillStatusMock.mockResolvedValue(
      candidateSkill({ status: "confirmed" }),
    );

    renderSection();
    await screen.findByText("Python");

    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() =>
      expect(updateCandidateSkillStatusMock).toHaveBeenCalledWith(
        ACCESS_TOKEN,
        "cs-1",
        "confirmed",
      ),
    );
    expect(await screen.findByText(/confirmed \(1\)/i)).toBeInTheDocument();
  });

  it("rejects a suggested skill and keeps it visible for restoring", async () => {
    listCandidateSkillsMock.mockResolvedValue([candidateSkill()]);
    updateCandidateSkillStatusMock.mockResolvedValue(
      candidateSkill({ status: "rejected" }),
    );

    renderSection();
    await screen.findByText("Python");

    fireEvent.click(screen.getByRole("button", { name: /reject/i }));

    await waitFor(() =>
      expect(updateCandidateSkillStatusMock).toHaveBeenCalledWith(
        ACCESS_TOKEN,
        "cs-1",
        "rejected",
      ),
    );
    // Rejection is a tombstone, not a delete — the row stays, with a
    // Restore action instead of Confirm.
    expect(await screen.findByText(/rejected \(1\)/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /restore/i }),
    ).toBeInTheDocument();
  });

  it("adds a skill manually and refreshes the list", async () => {
    listCandidateSkillsMock
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        candidateSkill({ skill_name: "Docker", status: "confirmed" }),
      ]);
    addCandidateSkillMock.mockResolvedValue(
      candidateSkill({ skill_name: "Docker", status: "confirmed" }),
    );

    renderSection();
    await screen.findByText(/no skills yet/i);

    fireEvent.change(screen.getByLabelText(/add a skill/i), {
      target: { value: "Docker" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    await waitFor(() =>
      expect(addCandidateSkillMock).toHaveBeenCalledWith(
        ACCESS_TOKEN,
        "Docker",
      ),
    );
    expect(await screen.findByText("Docker")).toBeInTheDocument();
  });

  it("shows the API's message when a skill is not in the taxonomy", async () => {
    const { ApiError } = await import("@/lib/api-client");
    listCandidateSkillsMock.mockResolvedValue([]);
    addCandidateSkillMock.mockRejectedValue(
      new ApiError(422, "'Rust' is not in the skill taxonomy"),
    );

    renderSection();
    await screen.findByText(/no skills yet/i);

    fireEvent.change(screen.getByLabelText(/add a skill/i), {
      target: { value: "Rust" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "'Rust' is not in the skill taxonomy",
    );
  });

  it("surfaces an error when confirming fails, without changing the row", async () => {
    const { ApiError } = await import("@/lib/api-client");
    listCandidateSkillsMock.mockResolvedValue([candidateSkill()]);
    updateCandidateSkillStatusMock.mockRejectedValue(
      new ApiError(403, "not authorized to access this candidate skill"),
    );

    renderSection();
    await screen.findByText("Python");

    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "not authorized to access this candidate skill",
    );
    expect(screen.getByText(/needs review \(1\)/i)).toBeInTheDocument();
  });

  it("refetches on demand", async () => {
    listCandidateSkillsMock.mockResolvedValue([]);

    renderSection();
    await screen.findByText(/no skills yet/i);
    expect(listCandidateSkillsMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() =>
      expect(listCandidateSkillsMock).toHaveBeenCalledTimes(2),
    );
  });

  it("renders evidence with no excerpt without inventing one", async () => {
    listCandidateSkillsMock.mockResolvedValue([
      candidateSkill({
        status: "confirmed",
        evidence: [
          {
            id: "ev-manual",
            source_type: "manual" as const,
            source_identifier: "user-1",
            excerpt: null,
            extraction_method: "manual_entry",
            confidence: 1,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
      }),
    ]);

    renderSection();

    expect(await screen.findByText(/added by you/i)).toBeInTheDocument();
    expect(screen.getByText(/100% confidence/)).toBeInTheDocument();
  });
});
