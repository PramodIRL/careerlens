import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
// 7.1c: the dashboard now relays saved-job changes from JobsSection
// to RoadmapSection, so both sections' reads have to be mocked for
// that wiring to be observable.
const listSavedJobsMock = vi.fn();
const deleteSavedJobMock = vi.fn();
const updateSavedJobMock = vi.fn();
const getRoadmapMock = vi.fn();

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
    listSavedJobs: (...args: unknown[]) => listSavedJobsMock(...args),
    deleteSavedJob: (...args: unknown[]) => deleteSavedJobMock(...args),
    updateSavedJob: (...args: unknown[]) => updateSavedJobMock(...args),
    getRoadmap: (...args: unknown[]) => getRoadmapMock(...args),
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
  listSavedJobsMock.mockReset().mockResolvedValue([]);
  deleteSavedJobMock.mockReset();
  updateSavedJobMock.mockReset();
  getRoadmapMock.mockReset();
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

// =====================================================================
// 7.1c — the dashboard relays saved-job changes to the roadmap
//
// THE WIRING ITSELF. jobs-section.test.tsx proves the callback fires and
// roadmap-section.test.tsx proves the section reacts to `jobsVersion`;
// neither can prove the dashboard actually connects the two, which is
// exactly what was missing and what left the roadmap stuck on a stale
// count until the page was reloaded.
// =====================================================================

describe("dashboard saved-job coordination", () => {
  beforeEach(() => {
    refreshMock.mockResolvedValue({ access_token: "tok" });
    getCurrentUserMock.mockResolvedValue(MOCK_USER);
    listResumesMock.mockResolvedValue([]);
  });

  // One week, one item — enough for the plan to be visibly on screen
  // before the edit and visibly gone after it. The full rendering is
  // roadmap-section.test.tsx's business.
  const PLAN = {
    formula_version: "roadmap_priority_v1",
    schedule_version: "roadmap_schedule_v2",
    narrative_schema_version: "roadmap_narrative_v1",
    narrative_status: "generated",
    reason: null,
    provider: "mock",
    selected_job_count: 1,
    saved_job_count: 1,
    has_selected_jobs: true,
    duration_days: 7,
    hours_per_day: 1,
    total_hours: 7,
    scheduled_days: 7,
    unscheduled_days: 0,
    coverage: "full",
    overview: "A plan for the week ahead.",
    weeks: [
      {
        week: 1,
        label: "Week 1 \u00b7 Days 1\u20137",
        start_day: 1,
        end_day: 7,
        focus: "Working on AWS",
        checkpoint: "You can explain what you built with AWS.",
        items: [
          {
            item_id: "skill-1",
            skill_id: "skill-1",
            skill_name: "AWS",
            state: "missing_required",
            start_day: 1,
            end_day: 7,
            week: 1,
            score: 105,
            state_weight: 100,
            recurrence: 5,
            why: "AWS is missing and is required by 1 of your 1 selected job.",
            affected_jobs: [
              {
                saved_job_id: "job-1",
                title: "Junior Backend Engineer",
                company: "Fictional Widgets Ltd",
                priority_rank: 1,
                match_score: 40,
              },
            ],
            evidence: [],
            estimated_hours: 7,
            steps: [],
            task: "Build something small with AWS.",
            outcome: "A running project that uses AWS.",
            success_criteria: "Someone else can run it from your notes.",
          },
        ],
      },
    ],
  };

  const savedJob = {
    id: "job-1",
    company: "Fictional Widgets Ltd",
    title: "Junior Backend Engineer",
    location: null,
    employment_type: null,
    source_url: null,
    description: "Build internal services.",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };

  it("lowers the roadmap's ceiling when a job is deleted", async () => {
    // THE SAME GUARANTEE, WITHOUT THE SECOND REQUEST (7.2 F5). The
    // roadmap's ceiling still has to come down when a job goes, and only
    // the dashboard can tell it so — but the count now travels as a prop
    // from the list JobsSection already holds, rather than through a
    // refetch of an endpoint that was just read.
    listSavedJobsMock.mockResolvedValue([savedJob]);
    deleteSavedJobMock.mockResolvedValue(undefined);

    renderDashboard();
    await screen.findByText("Junior Backend Engineer");
    expect(await screen.findByText(/of 1 saved job/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    expect(await screen.findByText(/of 0 saved jobs/)).toBeInTheDocument();
  }, 10000);

  it("lists the saved jobs exactly once for the whole page", async () => {
    // THE DUPLICATE THIS REPLACED. Two sections used to read the same
    // endpoint on mount — JobsSection for the list it renders and
    // RoadmapSection for its length — so the count was TWO, and a delete
    // made it three. One section owns the request now and reports the
    // number, so the correct count is ONE, and it stays one across the
    // mutation that used to add a read.
    listSavedJobsMock.mockResolvedValue([savedJob]);
    deleteSavedJobMock.mockResolvedValue(undefined);

    renderDashboard();
    await screen.findByText("Junior Backend Engineer");
    await waitFor(() => expect(listSavedJobsMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    // The delete is applied in place, so nothing refetches the list —
    // and the roadmap still learns the new count.
    await waitFor(() =>
      expect(screen.queryByText("Junior Backend Engineer")).toBeNull(),
    );
    expect(await screen.findByText(/of 0 saved jobs/)).toBeInTheDocument();
    // NO LOOP: reporting the count upward must not feed back into
    // another read.
    expect(listSavedJobsMock).toHaveBeenCalledTimes(1);
  }, 10000);

  it("withdraws the roadmap when a job description is edited", async () => {
    // AN EDIT MOVES NO JOB AND CHANGES NO COUNT, and is still a change
    // the roadmap has to hear about: the description is what the server
    // derives that job's skill requirements from, so editing it moves
    // the gaps the plan was built out of. A plan drawn beforehand
    // describes requirements that no longer exist.
    listSavedJobsMock.mockResolvedValue([savedJob]);
    getRoadmapMock.mockResolvedValue(PLAN);
    updateSavedJobMock.mockResolvedValue({
      ...savedJob,
      description: "Now asks for Kubernetes instead.",
    });

    renderDashboard();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: /generate roadmap/i }));
    expect(await screen.findByText("AWS")).toBeInTheDocument();
    const readsBefore = listSavedJobsMock.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", {
      name: "Edit Junior Backend Engineer",
    });
    fireEvent.change(within(form).getByLabelText("Job description"), {
      target: { value: "Now asks for Kubernetes instead." },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSavedJobMock).toHaveBeenCalled());

    // The plan is withdrawn rather than left up describing gaps derived
    // from a description that no longer exists.
    await waitFor(() => expect(screen.queryByText("AWS")).toBeNull());
    expect(
      screen.getByText(/saved jobs changed after this plan was made/i),
    ).toBeInTheDocument();
    // AND IT COST NOTHING TO LEARN. An edit moves no count, so there is
    // nothing to re-read: `jobsVersion` alone retires the plan.
    expect(listSavedJobsMock.mock.calls.length).toBe(readsBefore);
  }, 10000);
});
