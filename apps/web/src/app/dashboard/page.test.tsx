import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const refreshMock = vi.fn();
const getCurrentUserMock = vi.fn();
const logoutMock = vi.fn();
const getProfileMock = vi.fn();
const listResumesMock = vi.fn();
const listCandidateSkillsMock = vi.fn();
const getSkillProfileMock = vi.fn();
const getGitHubConnectionMock = vi.fn();
const deleteResumeMock = vi.fn();
const disconnectGitHubMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    refresh: (...args: unknown[]) => refreshMock(...args),
    getCurrentUser: (...args: unknown[]) => getCurrentUserMock(...args),
    logout: (...args: unknown[]) => logoutMock(...args),
    // The dashboard renders <ProfileForm> and <ResumeSection>, which
    // each fetch their own data on mount — both must be mocked too, or
    // they hit a real (nonexistent, in this test) API. Their own
    // behavior is covered by profile-form.test.tsx and
    // resume-section.test.tsx.
    getProfile: (...args: unknown[]) => getProfileMock(...args),
    listResumes: (...args: unknown[]) => listResumesMock(...args),
    // Prompt 3.4 + auto-refresh: the dashboard also renders
    // <SkillsSection>, <SkillProfileSection> and <GitHubSection>, and
    // now coordinates refreshes between them.
    listCandidateSkills: (...args: unknown[]) =>
      listCandidateSkillsMock(...args),
    getSkillProfile: (...args: unknown[]) => getSkillProfileMock(...args),
    getGitHubConnection: (...args: unknown[]) =>
      getGitHubConnectionMock(...args),
    deleteResume: (...args: unknown[]) => deleteResumeMock(...args),
    disconnectGitHub: (...args: unknown[]) => disconnectGitHubMock(...args),
  };
});

import { AuthProvider } from "@/lib/auth-context";
import DashboardPage from "./page";

const MOCK_USER = {
  id: "1",
  email: "alice@example.com",
  created_at: "2026-01-01T00:00:00Z",
};

const EMPTY_PROFILE = {
  summary: {
    total: 0,
    confirmed: 0,
    suggested: 0,
    rejected: 0,
    by_source: { resume: 0, github: 0, manual: 0 },
    multi_source: 0,
    reviewed: true,
  },
  skills: [],
};

function renderDashboard() {
  return render(
    <AuthProvider>
      <DashboardPage />
    </AuthProvider>,
  );
}

beforeEach(() => {
  pushMock.mockClear();
  refreshMock.mockReset();
  getCurrentUserMock.mockReset();
  logoutMock.mockReset();
  getProfileMock.mockReset().mockResolvedValue({
    user_id: MOCK_USER.id,
    full_name: null,
    headline: null,
    city: null,
    country: null,
    experience_level: null,
    target_roles: [],
    target_skills: [],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  });
  listResumesMock.mockReset().mockResolvedValue([]);
  // Sensible defaults for the other sections the dashboard renders, so
  // every test starts from a quiet, fully-mocked page.
  getGitHubConnectionMock.mockReset().mockResolvedValue(null);
  listCandidateSkillsMock.mockReset().mockResolvedValue([]);
  getSkillProfileMock.mockReset().mockResolvedValue(EMPTY_PROFILE);
  deleteResumeMock.mockReset().mockResolvedValue(undefined);
  disconnectGitHubMock.mockReset().mockResolvedValue(undefined);
});

describe("dashboard protected navigation", () => {
  it("redirects to /login when there is no valid session", async () => {
    refreshMock.mockRejectedValue(new Error("no session"));

    renderDashboard();

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/login"));
  });

  it("renders the dashboard, without redirecting, when authenticated", async () => {
    refreshMock.mockResolvedValue({
      access_token: "tok",
      token_type: "bearer",
      expires_in: 900,
    });
    getCurrentUserMock.mockResolvedValue(MOCK_USER);

    renderDashboard();

    expect(await screen.findByText(/alice@example\.com/)).toBeInTheDocument();
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("logs out and redirects to /login exactly once", async () => {
    refreshMock.mockResolvedValue({
      access_token: "tok",
      token_type: "bearer",
      expires_in: 900,
    });
    getCurrentUserMock.mockResolvedValue(MOCK_USER);
    logoutMock.mockResolvedValue(undefined);

    renderDashboard();
    await screen.findByText(/alice@example\.com/);

    screen.getByRole("button", { name: /log out/i }).click();

    await waitFor(() => expect(logoutMock).toHaveBeenCalled());
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/login"));

    // Regression guard: handleLogout used to call router.push("/login")
    // itself *in addition to* the unauthenticated-status effect above
    // also pushing once logout() flipped status — two redirects racing
    // for the same navigation, which could leave the real Next.js
    // router's client-side transition stuck. There must be exactly one
    // push, from the effect alone, once everything above has settled.
    expect(pushMock).toHaveBeenCalledTimes(1);
  });
});

// --- cross-section auto-refresh --------------------------------------

describe("dashboard refresh coordination", () => {
  // The file-level beforeEach already mocks every section; these tests
  // only need an authenticated session on top of it.
  beforeEach(() => {
    refreshMock.mockResolvedValue({ access_token: "tok" });
    getCurrentUserMock.mockResolvedValue(MOCK_USER);
  });

  it("refetches skills and profile when a resume finishes extracting", async () => {
    // The whole point of the change: the user uploads, waits, and the
    // skill sections update themselves — no Refresh click anywhere.
    const queued = {
      id: "r1",
      original_filename: "cv.pdf",
      content_type: "application/pdf",
      file_size_bytes: 1024,
      status: "queued" as const,
      error_message: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    listResumesMock
      .mockResolvedValueOnce([queued])
      .mockResolvedValue([{ ...queued, status: "succeeded" as const }]);

    renderDashboard();
    await screen.findByText("Queued");

    const skillsBefore = listCandidateSkillsMock.mock.calls.length;
    const profileBefore = getSkillProfileMock.mock.calls.length;

    // Extraction completes on the next poll tick.
    await screen.findByText("Ready", {}, { timeout: 4000 });

    await waitFor(
      () => {
        expect(listCandidateSkillsMock.mock.calls.length).toBeGreaterThan(
          skillsBefore,
        );
        expect(getSkillProfileMock.mock.calls.length).toBeGreaterThan(
          profileBefore,
        );
      },
      { timeout: 4000 },
    );
  }, 15000);

  it("does not refetch skills while nothing has completed", async () => {
    listResumesMock.mockResolvedValue([]);

    renderDashboard();
    await waitFor(() => expect(listCandidateSkillsMock).toHaveBeenCalled());
    const skillsCalls = listCandidateSkillsMock.mock.calls.length;

    await new Promise((resolve) => setTimeout(resolve, 2500));

    expect(listCandidateSkillsMock.mock.calls.length).toBe(skillsCalls);
  }, 10000);
});

// --- removal propagates too (delete / disconnect) --------------------

describe("dashboard refresh coordination on removal", () => {
  beforeEach(() => {
    refreshMock.mockResolvedValue({ access_token: "tok" });
    getCurrentUserMock.mockResolvedValue(MOCK_USER);
  });

  const READY_RESUME = {
    id: "r1",
    original_filename: "cv.pdf",
    content_type: "application/pdf",
    file_size_bytes: 1024,
    status: "succeeded" as const,
    error_message: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };

  it("refetches skills and profile when a resume is deleted", async () => {
    listResumesMock.mockResolvedValue([READY_RESUME]);

    renderDashboard();
    await screen.findByText("cv.pdf");
    const skillsBefore = listCandidateSkillsMock.mock.calls.length;
    const profileBefore = getSkillProfileMock.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(listCandidateSkillsMock.mock.calls.length).toBeGreaterThan(
        skillsBefore,
      );
      expect(getSkillProfileMock.mock.calls.length).toBeGreaterThan(
        profileBefore,
      );
    });
  }, 10000);

  it("refetches skills and profile when GitHub is disconnected", async () => {
    listResumesMock.mockResolvedValue([]);
    getGitHubConnectionMock.mockResolvedValue({
      username: "octocat",
      github_user_id: 1,
      public_repo_count: 3,
      last_verified_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });

    renderDashboard();
    const button = await screen.findByRole("button", { name: "Disconnect" });
    const skillsBefore = listCandidateSkillsMock.mock.calls.length;
    const profileBefore = getSkillProfileMock.mock.calls.length;

    fireEvent.click(button);

    await waitFor(() => {
      expect(listCandidateSkillsMock.mock.calls.length).toBeGreaterThan(
        skillsBefore,
      );
      expect(getSkillProfileMock.mock.calls.length).toBeGreaterThan(
        profileBefore,
      );
    });
  }, 10000);
});
