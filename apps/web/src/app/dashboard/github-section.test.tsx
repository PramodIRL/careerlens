import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getGitHubConnectionMock = vi.fn();
const connectGitHubMock = vi.fn();
const disconnectGitHubMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getGitHubConnection: (...args: unknown[]) =>
      getGitHubConnectionMock(...args),
    connectGitHub: (...args: unknown[]) => connectGitHubMock(...args),
    disconnectGitHub: (...args: unknown[]) => disconnectGitHubMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";

import GitHubSection from "./github-section";

const ACCESS_TOKEN = "tok";

function connection(overrides: Record<string, unknown> = {}) {
  return {
    user_id: "user-1",
    username: "Octocat",
    github_user_id: 583231,
    public_repo_count: 8,
    last_verified_at: "2026-08-24T10:00:00Z",
    created_at: "2026-08-24T10:00:00Z",
    updated_at: "2026-08-24T10:00:00Z",
    ...overrides,
  };
}

function renderSection() {
  return render(<GitHubSection accessToken={ACCESS_TOKEN} />);
}

beforeEach(() => {
  getGitHubConnectionMock.mockReset();
  connectGitHubMock.mockReset();
  disconnectGitHubMock.mockReset();
});

describe("github section", () => {
  it("shows the connect form when nothing is connected", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();

    expect(await screen.findByLabelText("GitHub username")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("shows the public-data-only notice before anything is connected", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();

    expect(
      await screen.findByText(/We only analyze public data/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/never asks for your GitHub password/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/never requests access to your private repositories/i),
    ).toBeInTheDocument();
  });

  it("keeps the public-data-only notice visible after connecting", async () => {
    // The promise is the point: a user who has already connected is
    // exactly the person who might later wonder what was shared. The
    // notice is not a one-time onboarding message.
    getGitHubConnectionMock.mockResolvedValue(connection());

    renderSection();

    expect(
      await screen.findByText(/We only analyze public data/i),
    ).toBeInTheDocument();
  });

  it("describes the username field with the public-data notice", async () => {
    // aria-describedby, so the promise reaches a screen-reader user as
    // part of the field rather than as unassociated text nearby.
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();

    const input = await screen.findByLabelText("GitHub username");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      /We only analyze public data/i,
    );
  });

  it("never renders a password field", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    const { container } = renderSection();
    await screen.findByLabelText("GitHub username");

    expect(container.querySelector('input[type="password"]')).toBeNull();
    // A browser must not offer to autofill a credential into the
    // username box either.
    expect(screen.getByLabelText("GitHub username")).toHaveAttribute(
      "autocomplete",
      "off",
    );
  });

  it("connects a username and shows the account", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockResolvedValue(connection());

    renderSection();
    const input = await screen.findByLabelText("GitHub username");

    fireEvent.change(input, { target: { value: "octocat" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("Octocat")).toBeInTheDocument();
    expect(connectGitHubMock).toHaveBeenCalledWith(ACCESS_TOKEN, "octocat");
    expect(screen.getByText("8 public repositories")).toBeInTheDocument();
    expect(screen.getByText(/Last checked/)).toBeInTheDocument();
  });

  it("links to the connected account on github.com", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());

    renderSection();

    const link = await screen.findByRole("link", { name: "Octocat" });
    expect(link).toHaveAttribute("href", "https://github.com/Octocat");
  });

  it("uses the singular form for a single repository", async () => {
    getGitHubConnectionMock.mockResolvedValue(
      connection({ public_repo_count: 1 }),
    );

    renderSection();

    expect(await screen.findByText("1 public repository")).toBeInTheDocument();
  });

  it("shows the API's message when the account does not exist", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockRejectedValue(
      new ApiError(404, "no public GitHub account found for 'nope'"),
    );

    renderSection();
    fireEvent.change(await screen.findByLabelText("GitHub username"), {
      target: { value: "nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no public GitHub account found for 'nope'",
    );
    // Still on the form — nothing was connected.
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("shows the API's message when GitHub is rate-limited", async () => {
    // The reset time is the actionable part, so it must survive to the
    // user rather than being flattened into "something went wrong".
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockRejectedValue(
      new ApiError(
        503,
        "GitHub's public API rate limit was reached. Try again after 15:30 UTC.",
      ),
    );

    renderSection();
    fireEvent.change(await screen.findByLabelText("GitHub username"), {
      target: { value: "octocat" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Try again after 15:30 UTC.",
    );
  });

  it("shows the API's message when GitHub times out", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockRejectedValue(
      new ApiError(
        504,
        "GitHub did not respond in time — try again in a moment",
      ),
    );

    renderSection();
    fireEvent.change(await screen.findByLabelText("GitHub username"), {
      target: { value: "octocat" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "GitHub did not respond in time",
    );
  });

  it("disconnects and returns to the connect form", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    disconnectGitHubMock.mockResolvedValue(undefined);

    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => {
      expect(screen.getByLabelText("GitHub username")).toBeInTheDocument();
    });
    expect(disconnectGitHubMock).toHaveBeenCalledWith(ACCESS_TOKEN);
    expect(screen.queryByText("Octocat")).not.toBeInTheDocument();
  });

  it("keeps the account visible when disconnecting fails", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    disconnectGitHubMock.mockRejectedValue(
      new ApiError(503, "service unavailable"),
    );

    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "service unavailable",
    );
    expect(screen.getByText("Octocat")).toBeInTheDocument();
  });

  it("surfaces an error when the initial load fails", async () => {
    getGitHubConnectionMock.mockRejectedValue(
      new ApiError(500, "server error"),
    );

    renderSection();

    expect(await screen.findByRole("alert")).toHaveTextContent("server error");
  });

  it("does not submit an empty username", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();
    await screen.findByLabelText("GitHub username");

    expect(screen.getByRole("button", { name: "Connect" })).toBeDisabled();
    expect(connectGitHubMock).not.toHaveBeenCalled();
  });
});
