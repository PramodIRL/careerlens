import { render, screen, waitFor } from "@testing-library/react";
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
  };
});

import { AuthProvider } from "@/lib/auth-context";
import DashboardPage from "./page";

const MOCK_USER = {
  id: "1",
  email: "alice@example.com",
  created_at: "2026-01-01T00:00:00Z",
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

  it("logs out and redirects to /login", async () => {
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
  });
});
