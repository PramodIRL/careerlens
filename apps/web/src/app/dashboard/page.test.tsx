import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const refreshMock = vi.fn();
const getCurrentUserMock = vi.fn();
const logoutMock = vi.fn();

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
