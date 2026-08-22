import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const loginMock = vi.fn();
const refreshMock = vi.fn();
const getCurrentUserMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    login: (...args: unknown[]) => loginMock(...args),
    refresh: (...args: unknown[]) => refreshMock(...args),
    // login() in AuthProvider always follows a successful token exchange
    // with getCurrentUser() to populate the user — must be mocked too,
    // or it hits a real (nonexistent, in this test) API and rejects.
    getCurrentUser: (...args: unknown[]) => getCurrentUserMock(...args),
  };
});

import { AuthProvider } from "@/lib/auth-context";
import LoginPage from "./page";

function renderLoginPage() {
  return render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
}

async function submit(email: string, password: string) {
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: email },
  });
  fireEvent.change(screen.getByLabelText(/password/i), {
    target: { value: password },
  });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
}

beforeEach(() => {
  pushMock.mockClear();
  loginMock.mockReset();
  getCurrentUserMock.mockReset();
  // AuthProvider always attempts a silent refresh on mount; irrelevant
  // to these tests, so keep it a harmless no-op failure by default.
  refreshMock.mockReset().mockRejectedValue(new Error("no session"));
});

describe("login page", () => {
  it("logs in and redirects to /dashboard on success", async () => {
    loginMock.mockResolvedValue({
      access_token: "tok",
      token_type: "bearer",
      expires_in: 900,
    });
    getCurrentUserMock.mockResolvedValue({
      id: "1",
      email: "alice@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });

    renderLoginPage();
    await submit("alice@example.com", "correct-password");

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    expect(loginMock).toHaveBeenCalledWith(
      "alice@example.com",
      "correct-password",
    );
  });

  it("shows the API's error message and does not redirect on failure", async () => {
    const { ApiError } = await import("@/lib/api-client");
    loginMock.mockRejectedValue(new ApiError(401, "invalid email or password"));

    renderLoginPage();
    await submit("alice@example.com", "wrong-password");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "invalid email or password",
    );
    expect(pushMock).not.toHaveBeenCalled();
  });
});
