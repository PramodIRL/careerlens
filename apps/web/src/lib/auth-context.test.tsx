/**
 * Regression coverage for a real, live-browser-confirmed race: on mount,
 * AuthProvider fires a silent `refresh()` to restore a session from the
 * HttpOnly cookie, if any. If a user submits the login form before that
 * silent refresh has settled, and it settles *after* login() already
 * succeeded, its unconditional `setStatus(...)` used to clobber the real,
 * more recent session — with no error, console warning, or crash: just a
 * silently reverted `status`. See docs/decisions.md.
 *
 * This can't be reliably forced through a live browser or a fully-mocked
 * `next/navigation` unit test (both depend on real timing this suite
 * doesn't control) — a manually-sequenced promise, the same technique
 * `api-client.test.ts` uses for `refresh()`'s own dedup behavior, proves
 * the fix deterministically instead.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const refreshMock = vi.fn();
const loginMock = vi.fn();
const getCurrentUserMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    refresh: (...args: unknown[]) => refreshMock(...args),
    login: (...args: unknown[]) => loginMock(...args),
    getCurrentUser: (...args: unknown[]) => getCurrentUserMock(...args),
  };
});

import { AuthProvider, useAuth } from "@/lib/auth-context";

const MOCK_USER = {
  id: "1",
  email: "alice@example.com",
  created_at: "2026-01-01T00:00:00Z",
};

/** Minimal consumer exposing status as plain text and a way to trigger
 * login(), so the test can drive AuthProvider directly without going
 * through a page component. */
function StatusProbe() {
  const { status, login } = useAuth();

  async function handleLogin() {
    await login("alice@example.com", "correct-password");
  }

  return (
    <div>
      <p>status: {status}</p>
      <button onClick={handleLogin}>Log in</button>
    </div>
  );
}

function renderProbe() {
  return render(
    <AuthProvider>
      <StatusProbe />
    </AuthProvider>,
  );
}

beforeEach(() => {
  refreshMock.mockReset();
  loginMock.mockReset();
  getCurrentUserMock.mockReset();
});

describe("AuthProvider", () => {
  it("does not let a late-resolving mount-time refresh clobber a successful login", async () => {
    let rejectRefresh: (reason: unknown) => void = () => {};
    refreshMock.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectRefresh = reject;
      }),
    );
    loginMock.mockResolvedValue({
      access_token: "tok",
      token_type: "bearer",
      expires_in: 900,
    });
    getCurrentUserMock.mockResolvedValue(MOCK_USER);

    renderProbe();
    expect(screen.getByText("status: loading")).toBeInTheDocument();

    // Explicit login completes *while the initial silent refresh above
    // is still pending* — this is the race.
    screen.getByRole("button", { name: /log in/i }).click();
    await waitFor(() =>
      expect(screen.getByText("status: authenticated")).toBeInTheDocument(),
    );

    // The stale initial refresh — kicked off on mount, before this login
    // ever happened — finally settles now, rejecting (no cookie was ever
    // present). It must not revert the session login already established.
    await act(async () => {
      rejectRefresh(new Error("no session"));
      // Give the (buggy, pre-fix) .catch handler's synchronous setState
      // a couple of microtask hops to land before asserting it didn't.
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("status: authenticated")).toBeInTheDocument();
    expect(getCurrentUserMock).toHaveBeenCalledTimes(1); // only from login, never from refresh
  });
});
