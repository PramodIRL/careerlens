import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const registerMock = vi.fn();
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
    register: (...args: unknown[]) => registerMock(...args),
    login: (...args: unknown[]) => loginMock(...args),
    refresh: (...args: unknown[]) => refreshMock(...args),
    // login() in AuthProvider always follows a successful token exchange
    // with getCurrentUser() to populate the user — must be mocked too,
    // or it hits a real (nonexistent, in this test) API and rejects.
    getCurrentUser: (...args: unknown[]) => getCurrentUserMock(...args),
  };
});

import { AuthProvider } from "@/lib/auth-context";
import RegisterPage from "./page";

function renderRegisterPage() {
  return render(
    <AuthProvider>
      <RegisterPage />
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
  fireEvent.click(screen.getByRole("button", { name: /create account/i }));
}

beforeEach(() => {
  pushMock.mockClear();
  registerMock.mockReset();
  loginMock.mockReset();
  getCurrentUserMock.mockReset();
  refreshMock.mockReset().mockRejectedValue(new Error("no session"));
});

describe("register page", () => {
  it("registers, auto-logs in, and redirects to /dashboard on success", async () => {
    registerMock.mockResolvedValue({
      id: "1",
      email: "alice@example.com",
      created_at: "2026-01-01T00:00:00Z",
    });
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

    renderRegisterPage();
    await submit("alice@example.com", "a-valid-password");

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    expect(registerMock).toHaveBeenCalledWith(
      "alice@example.com",
      "a-valid-password",
    );
    expect(loginMock).toHaveBeenCalledWith(
      "alice@example.com",
      "a-valid-password",
    );
  });

  it("shows a duplicate-email error and does not redirect", async () => {
    const { ApiError } = await import("@/lib/api-client");
    registerMock.mockRejectedValue(
      new ApiError(409, "email already registered"),
    );

    renderRegisterPage();
    await submit("alice@example.com", "a-valid-password");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "email already registered",
    );
    expect(loginMock).not.toHaveBeenCalled();
    expect(pushMock).not.toHaveBeenCalled();
  });
});
